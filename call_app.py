import os
import logging
import asyncio
import json
from datetime import datetime
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, firestore
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware # For CORS
from twilio.twiml.voice_response import VoiceResponse, Start
from hume import HumeStreamClient, HumeClientException
from hume.models.config import LanguageConfig
import boto3

# --- 1. Initialization & Config ---

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s')

# Initialize FastAPI app
app = FastAPI()

# --- 2. CORS Middleware ---
# This allows your local web app (on 127.0.0.1:8080) to talk to your Render app.
origins = [
    "http://127.0.0.1:8080",
    "http://localhost:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods (GET, POST, etc.)
    allow_headers=["*"], # Allows all headers
)

# --- 3. Firebase Initialization ---
db = None
try:
    # This automatically finds and uses the GOOGLE_APPLICATION_CREDENTIALS env variable
    # which Render sets from your Secret File.
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        logging.warning("GOOGLE_APPLICATION_CREDENTIALS not set. Firebase may not initialize.")
        # Attempt default initialization (might work in some GCP environments)
        firebase_admin.initialize_app()
    else:
        # Check if the path points to a file that exists (for Render Secret Files)
        if os.path.exists(cred_path):
             cred = credentials.Certificate(cred_path)
             firebase_admin.initialize_app(cred)
        else:
            # Fallback for environments where the variable *is* the JSON content
             logging.info("Credential path doesn't exist. Trying to parse as JSON string.")
             cred_json = json.loads(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON_CONTENT", "{}"))
             if not cred_json:
                raise Exception("No valid Firebase credentials found.")
             cred = credentials.Certificate(cred_json)
             firebase_admin.initialize_app(cred)

    db = firestore.client()
    logging.info("Firebase Firestore client initialized successfully.")
except Exception as e:
    logging.error(f"CRITICAL: Failed to initialize Firebase. App may not function. Error: {e}", exc_info=True)
    db = None # Ensure db is None if init fails

# --- 4. Global State ---
# Dictionary to store active WebSocket connections and Hume clients
active_connections = {}

# --- 5. Helper Functions ---

async def generate_system_prompt(patient_data, clinic_name, base_prompt):
    """
    Generates a dynamic system prompt for the Hume EVI based on the patient's
    scheduled encounters and medical data.
    Fetches relevant knowledge base articles from Firestore.
    """
    if not db:
        logging.error("Firestore DB not available in generate_system_prompt.")
        return base_prompt # Return default prompt

    # Start with the base prompt (Identity + Rules)
    prompt_sections = [base_prompt]
    
    # Default task if no specific action is found
    task_prompt = f"You are calling {patient_data.get('name', 'the patient')}. Start by introducing yourself and asking how they are doing today."
    
    # Look for a scheduled AI phone call
    scheduled_ai_call = None
    if 'encounters' in patient_data and patient_data['encounters']:
        for date, encounter in patient_data['encounters'].items():
            if (encounter and isinstance(encounter, dict) and
                encounter.get('status') == 'scheduled' and 
                encounter.get('type') == 'AI phone call'):
                scheduled_ai_call = encounter
                scheduled_ai_call['date'] = date # Store the date for saving results
                break # Found the first scheduled call

    if scheduled_ai_call:
        purpose = scheduled_ai_call.get('purpose', '').lower()
        patient_name = patient_data.get('name', 'the patient')
        
        knowledge_base_doc = None
        
        try:
            if "medication adherence" in purpose:
                meds = ", ".join(patient_data.get('medications', []))
                if not meds:
                    meds = "your new medications"
                
                task_prompt = (
                    f"TASK: You are calling {patient_name} for a medication adherence check-in. "
                    f"Your goal is to: "
                    f"1. Gently ask if they have been able to take their medications, specifically {meds}, as prescribed. "
                    f"2. Ask if they are experiencing any side effects or have any questions about them. "
                    f"3. Follow the clinic protocol for this task."
                )
                knowledge_base_doc = await asyncio.to_thread(
                    db.collection('prompt_library').document('kb_medication_adherence').get
                )

            elif "post-op check-in" in purpose:
                procedures = ", ".join(patient_data.get('procedures_history', []))
                if not procedures:
                    procedures = "your recent procedure"
                
                task_prompt = (
                    f"TASK: You are calling {patient_name} for a post-operative check-in regarding their {procedures}. "
                    f"Your goal is to: "
                    f"1. Ask how they are feeling and recovering. "
                    f"2. Specifically ask about their pain level (e.g., on a scale of 1-10). "
                    f"3. Ask if they've noticed any signs of infection, like fever or redness. "
                    f"4. Follow the clinic protocol for this task."
                )
                knowledge_base_doc = await asyncio.to_thread(
                    db.collection('prompt_library').document('kb_post_op_checkin').get
                )
                
            else:
                # Fallback for other scheduled AI calls
                task_prompt = (
                    f"TASK: You are calling {patient_name} for a scheduled health check-in. "
                    f"The purpose of the call is: {scheduled_ai_call.get('purpose', 'a general check-in')}. "
                    f"Please begin the conversation and ask how they are doing."
                )

            # Add the task to the prompt
            prompt_sections.append(task_prompt)
            
            # Add the knowledge base article if we found one
            if knowledge_base_doc and knowledge_base_doc.exists:
                knowledge_content = knowledge_base_doc.to_dict().get('content', '')
                prompt_sections.append(f"KNOWLEDGE_BASE_PROTOCOL:\n{knowledge_content}")

        except Exception as e:
            logging.error(f"Error fetching knowledge base from Firestore: {e}", exc_info=True)
            # Continue without the KB article
            
    else:
        # This is an unscheduled call, just use the default task
        prompt_sections.append(task_prompt)
        
    # Combine all parts into the final system prompt
    final_prompt = "\n\n".join(prompt_sections)
    
    logging.info(f"Generated system prompt: {final_prompt[:150]}...") # Log first 150 chars
    return final_prompt, scheduled_ai_call

async def save_call_results_to_firestore(patient_data, call_sid, transcript, scheduled_encounter):
    """
    Saves the call transcript and updates the encounter in Firestore.
    """
    if not db:
        logging.error("Firestore DB not available in save_call_results.")
        return

    try:
        clinic_id = patient_data['clinic_id']
        mrn = patient_data['mrn']
        
        logging.info(f"Saving call results for MRN: {mrn} in Clinic: {clinic_id}")
        
        # Get the patient document reference
        patient_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)
        
        # Use a transaction to safely read and update the patient document
        @firestore.transactional
        def update_in_transaction(transaction, patient_ref):
            # Get the current patient data
            snapshot = patient_ref.get(transaction=transaction)
            if not snapshot.exists:
                logging.error(f"Patient {mrn} not found during transaction.")
                return
            
            patient_doc = snapshot.to_dict()
            encounters = patient_doc.get('encounters', {})

            if scheduled_encounter and scheduled_encounter['date'] in encounters:
                # Case 1: We have a scheduled encounter. Update it.
                encounter_date = scheduled_encounter['date']
                logging.info(f"Updating scheduled encounter for date: {encounter_date}")
                
                # Update the specific encounter's fields
                # Use . notation for updating fields within a map
                transaction.update(patient_ref, {
                    f'encounters.{encounter_date}.status': 'completed',
                    f'encounters.{encounter_date}.call_sid': call_sid,
                    f'encounters.{encounter_date}.call_transcript': transcript
                })
            else:
                # Case 2: No scheduled encounter found (or it was an ad-hoc call).
                # Create a new encounter for today.
                today_date = datetime.now().strftime('%Y-%m-%d')
                encounter_time = datetime.now().strftime('%H:%M:%S')
                new_encounter_key = f"{today_date}_{encounter_time}"
                
                logging.info(f"Creating new ad-hoc encounter with key: {new_encounter_key}")
                
                new_encounter_data = {
                    'status': 'completed',
                    'type': 'AI phone call',
                    'purpose': 'Ad-hoc follow-up call',
                    'call_sid': call_sid,
                    'call_transcript': transcript
                }
                
                # Add this new encounter to the encounters map
                encounters[new_encounter_key] = new_encounter_data
                transaction.update(patient_ref, {'encounters': encounters})

        transaction = db.transaction()
        await asyncio.to_thread(update_in_transaction, transaction, patient_ref)
        logging.info(f"Successfully saved call results for CallSid: {call_sid}")

    except Exception as e:
        logging.error(f"Error saving call results for CallSid {call_sid}: {e}", exc_info=True)

async def start_hume_evi_conversation(client: HumeStreamClient, call_sid: str, patient_data):
    """
    Manages the Hume EVI conversation WebSocket.
    Now saves the transcript at the end.
    """
    transcript = [] # To store the conversation
    scheduled_encounter_to_update = patient_data.get('scheduled_ai_call', None)

    try:
        async with client.connect() as websocket:
            logging.info(f"Hume EVI connection established for CallSid: {call_sid}")
            
            # Store the Hume client so the Twilio WS can send audio to it
            active_connections[call_sid]['hume_client'] = websocket
            
            async for message in websocket:
                if message.get("type") == "user_message":
                    msg = message.get("message", {}).get("content", "")
                    transcript.append(f"Patient: {msg}")
                elif message.get("type") == "assistant_message":
                    msg = message.get("message", {}).get("content", "")
                    transcript.append(f"Assistant: {msg}")
                elif message.get("type") == "audio_output":
                    # Send audio from Hume to Twilio
                    audio_chunk = message.get("data")
                    if audio_chunk:
                        await active_connections[call_sid]['twilio_ws'].send_json({
                            "event": "media",
                            "streamSid": active_connections[call_sid]['stream_sid'],
                            "media": {
                                "payload": audio_chunk
                            }
                        })
                elif message.get("type") == "error":
                    logging.error(f"Hume EVI Error for CallSid {call_sid}: {message.get('error')}")
                    transcript.append(f"HUME_ERROR: {message.get('error')}")
                elif message.get("type") == "conversation_end":
                    logging.info(f"Hume EVI conversation_end received for CallSid: {call_sid}")
                    transcript.append("INFO: Conversation ended.")
                    break
                    
    except HumeClientException as e:
        logging.error(f"Hume connection error for CallSid {call_sid}: {e}", exc_info=True)
        transcript.append(f"HUME_EXCEPTION: {e}")
    except Exception as e:
        logging.error(f"Error during Hume EVI conversation for CallSid {call_sid}: {e}", exc_info=True)
        transcript.append(f"SYSTEM_ERROR: {e}")
    finally:
        logging.info(f"Hume EVI conversation closing for CallSid: {call_sid}. Cleaning up.")
        
        # Save the full transcript to Firestore
        full_transcript = "\n".join(transcript)
        await save_call_results_to_firestore(patient_data, call_sid, full_transcript, scheduled_encounter_to_update)
        
        # Clean up the connection
        await cleanup_connection(call_sid, "Hume conversation ended")

async def cleanup_connection(call_sid: str, reason: str):
    """
    Closes WebSockets and removes the call from active_connections.
    """
    if call_sid in active_connections:
        logging.info(f"Cleaning up connections for CallSid: {call_sid} (Reason: {reason})")
        
        # Close Hume WebSocket if it exists
        hume_client = active_connections[call_sid].get('hume_client')
        if hume_client:
            try:
                # This might not be the correct way to close, depending on Hume's SDK
                # We'll just remove the reference.
                pass
            except Exception as e:
                logging.warning(f"Error closing Hume client for {call_sid}: {e}")

        # Close Twilio WebSocket if it exists
        twilio_ws = active_connections[call_sid].get('twilio_ws')
        if twilio_ws:
            try:
                await twilio_ws.close()
            except Exception as e:
                logging.warning(f"Error closing Twilio WS for {call_sid}: {e}")
        
        # Remove from active connections
        del active_connections[call_sid]
        logging.info(f"Successfully cleaned up CallSid: {call_sid}")
    else:
        logging.warning(f"Cleanup called for {call_sid}, but no active connection found.")

# --- 6. FastAPI Routes ---

@app.on_event("startup")
async def startup_event():
    """
    On startup, log that the app is running and check DB connection.
    """
    logging.info("FastAPI application startup...")
    if not db:
        logging.critical("Firestore DB is not available on startup. Most features will fail.")
    else:
        logging.info("FastAPI is up and connected to Firestore.")


@app.get("/wake-up")
async def wake_up():
    """
    A simple GET endpoint to wake up the Render service.
    """
    logging.info("GET /wake-up received. Server is awake.")
    return {"status": "awake"}


@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request, CallSid: str = Form(None)):
    """
    Handles incoming Twilio calls (both direct calls and click-to-call transfers).
    This function now generates a dynamic system prompt for Hume EVI.
    """
    logging.info(f"Twilio Call Webhook Received (CallSid: {CallSid})")
    
    if not db:
        logging.error("CRITICAL: Firestore DB not available. Cannot process call.")
        response = VoiceResponse()
        response.say("We are sorry, our system is experiencing database issues. Please call back later.")
        response.hangup()
        return PlainTextResponse(str(response), media_type='application/xml')
        
    try:
        form_data = await request.form()
        CallSid = form_data.get('CallSid', CallSid)
        
        # Get MRN and ClinicID from query params (passed by our website_app.py)
        mrn = request.query_params.get('mrn')
        clinic_id = request.query_params.get('clinic_id')

        if not CallSid:
            logging.warning("Request missing CallSid.")
            CallSid = f"UnknownCallSid_{datetime.now().isoformat()}" # Create a unique placeholder

        if not mrn or not clinic_id:
            logging.error(f"Missing MRN or ClinicID in request. CallSid: {CallSid}, MRN: {mrn}, ClinicID: {clinic_id}")
            response = VoiceResponse()
            response.say("Thank you for calling. This is an automated health service. We are unable to identify your patient record. Please contact your clinic directly. Goodbye.")
            response.hangup()
            return PlainTextResponse(str(response), media_type='application/xml')

        logging.info(f"Looking up patient for MRN: {mrn} in Clinic: {clinic_id}")
        
        # --- Fetch base prompts from Firestore ---
        try:
            prompt_identity_doc, prompt_rules_doc = await asyncio.gather(
                asyncio.to_thread(db.collection('prompt_library').document('prompt_identity').get),
                asyncio.to_thread(db.collection('prompt_library').document('prompt_rules').get)
            )
            
            if not prompt_identity_doc.exists or not prompt_rules_doc.exists:
                logging.error("CRITICAL: Base prompts (identity or rules) not found in Firestore 'prompt_library'.")
                raise Exception("Missing base prompts")
                
            base_prompt = (
                f"{prompt_identity_doc.to_dict().get('content', '')}\n\n"
                f"{prompt_rules_doc.to_dict().get('content', '')}"
            )
        except Exception as e:
            logging.error(f"Failed to fetch base prompts from Firestore: {e}", exc_info=True)
            response = VoiceResponse()
            response.say("We're sorry, there's an error with our AI configuration. Please contact the clinic.")
            response.hangup()
            return PlainTextResponse(str(response), media_type='application/xml')
        
        # Get patient data from Firestore
        patient_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)
        patient_doc = await asyncio.to_thread(patient_ref.get)

        if not patient_doc.exists:
            logging.error(f"Could not find patient for MRN {mrn} provided in webhook URL. CallSid: {CallSid}")
            response = VoiceResponse()
            response.say("We're sorry, we could not retrieve the patient records for this call. An application error occurred.")
            response.hangup()
            return PlainTextResponse(str(response), media_type='application/xml')

        patient_data = patient_doc.to_dict()
        patient_data['mrn'] = mrn # Add MRN to the dict
        patient_data['clinic_id'] = clinic_id # Add clinic_id
        
        # Get clinic name for the prompt
        clinic_info_doc = await asyncio.to_thread(db.collection('clinics').document(clinic_id).get)
        clinic_name = clinic_info_doc.to_dict().get('name', 'your clinic')

        # Generate a dynamic system prompt based on the patient's data
        system_prompt, scheduled_encounter = await generate_system_prompt(patient_data, clinic_name, base_prompt)
        
        # Pass the scheduled encounter info to the patient_data dict so we can find it later
        patient_data['scheduled_ai_call'] = scheduled_encounter

        # Start the WebSocket stream to Hume EVI
        try:
            logging.info(f"Connecting to Hume EVI for CallSid: {CallSid}")
            config = LanguageConfig(system_prompt=system_prompt)
            client = HumeStreamClient(os.environ.get("HUME_API_KEY"), config=config)
            
            # Store connection info *before* starting the task
            active_connections[CallSid] = {}
            
            # Start the EVI conversation
            asyncio.create_task(
                start_hume_evi_conversation(client, CallSid, patient_data)
            )

        except Exception as e:
            logging.error(f"Error starting Hume EVI connection for CallSid {CallSid}: {e}", exc_info=True)
            response = VoiceResponse()
            response.say("We're sorry, but we're unable to connect to our AI assistant at this time.")
            response.hangup()
            return PlainTextResponse(str(response), media_type='application/xml')
        
        # Connect Twilio to our WebSocket server
        response = VoiceResponse()
        start = Start()
        # Use the host from the request headers to build the WSS URL dynamically
        ws_url = f"wss://{request.headers['host']}/ws/{CallSid}"
        logging.info(f"Connecting Twilio to WebSocket: {ws_url}")
        start.stream(url=ws_url)
        response.append(start)
        response.pause(length=60) # Keep call alive for 60s
        
        logging.info(f"Twilio TwiML response sent for CallSid: {CallSid}")
        return PlainTextResponse(str(response), media_type='application/xml')

    except Exception as e:
        logging.error(f"Unexpected error in handle_incoming_call for CallSid {CallSid}: {e}", exc_info=True)
        response = VoiceResponse()
        response.say("An application error occurred. We apologize for the inconvenience.")
        response.hangup()
        return PlainTextResponse(str(response), media_type='application/xml')


@app.websocket("/ws/{call_sid}")
async def websocket_endpoint(websocket: WebSocket, call_sid: str):
    """
    WebSocket endpoint for Twilio Media Streams.
    It receives audio from Twilio and forwards it to Hume.
    """
    await websocket.accept()
    logging.info(f"Twilio WebSocket connected for CallSid: {call_sid}")
    
    if call_sid not in active_connections:
        logging.warning(f"No active connection found for CallSid {call_sid} upon WS connect. Creating one.")
        active_connections[call_sid] = {}
        
    active_connections[call_sid]['twilio_ws'] = websocket
    
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            
            if data['event'] == 'start':
                stream_sid = data['start']['streamSid']
                active_connections[call_sid]['stream_sid'] = stream_sid
                logging.info(f"Twilio stream started for CallSid: {call_sid}, StreamSid: {stream_sid}")
            
            elif data['event'] == 'media':
                # Get audio payload and send it to Hume
                payload = data['media']['payload']
                
                if call_sid in active_connections and 'hume_client' in active_connections[call_sid]:
                    hume_ws = active_connections[call_sid]['hume_client']
                    if hume_ws:
                        # Send audio data (base64) to Hume
                        await hume_ws.send_bytes(payload.encode('utf-8'))
                else:
                    logging.warning(f"No Hume client found for CallSid {call_sid}. Cannot forward audio.")
            
            elif data['event'] == 'stop':
                logging.info(f"Twilio stream stopped for CallSid: {call_sid}")
                break
                
    except WebSocketDisconnect:
        logging.info(f"Twilio WebSocket disconnected for CallSid: {call_sid}")
    except Exception as e:
        logging.error(f"Error in Twilio WebSocket handler for CallSid {call_sid}: {e}", exc_info=True)
    finally:
        # This finally block will run when Twilio hangs up or the stream stops
        await cleanup_connection(call_sid, "Twilio WebSocket closed")

