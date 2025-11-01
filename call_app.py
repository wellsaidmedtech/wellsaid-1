import os
import requests
import asyncio
import websockets
import json
import logging
import audioop
import base64
import wave
import io
import random
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, JSONResponse
from hume import HumeStreamClient
from hume.models.config import LanguageConfig
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client as TwilioRestClient
from twilio.base.exceptions import TwilioRestException
from pydantic import BaseModel
import firebase_admin # <-- NEW
from firebase_admin import credentials, firestore 
from dotenv import load_dotenv
load_dotenv() # This line actually reads the .env file
from fastapi.middleware.cors import CORSMiddleware

# Patient Data Import
try:
    from dummy_data import DUMMY_PATIENT_DB
except ImportError:
    print("Error: dummy_data.py not found. Please create it with the DUMMY_PATIENT_DB dictionary.")

# Clinic Helper Functions
def get_patient_data(clinic_id, mrn):
    """
    Retrieves a specific patient's data from a specific clinic.

    Args:
        clinic_id (str): The 5-digit ID of the clinic.
        mrn (str): The patient's Medical Record Number (MRN),
                   which is unique within that clinic.

    Returns:
        dict: A dictionary containing the patient's data if found,
              otherwise None.
    """
    print(f"Attempting to fetch data for Clinic ID: {clinic_id}, MRN: {mrn}")
    
    # 1. Find the clinic by its ID
    clinic = DUMMY_PATIENT_DB.get(clinic_id)
    
    if not clinic:
        print(f"Error: Clinic with ID '{clinic_id}' not found.")
        return None  # Clinic not found
    
    # 2. Find the patient within that clinic's patient dictionary
    # We use .get("patients", {}) to safely handle cases where a
    # clinic might exist but have no "patients" key (bad data).
    patient = clinic.get("patients", {}).get(mrn)
    
    if not patient:
        print(f"Error: Patient with MRN '{mrn}' not found in clinic '{clinic_id}'.")
        return None # Patient not found in this clinic

    print(f"Successfully found patient: {patient.get('name')}")
    return patient

def get_clinic_info(clinic_id):
    """
    Retrieves a clinic's general information (name, phone, etc.)
    excluding the full patient list.

    Args:
        clinic_id (str): The 5-digit ID of the clinic.

    Returns:
        dict: A dictionary with clinic info if found, otherwise None.
    """
    clinic_data = DUMMY_PATIENT_DB.get(clinic_id)
    
    if not clinic_data:
        print(f"Error: Clinic with ID '{clinic_id}' not found.")
        return None
        
    # Create a copy to avoid modifying the original
    # and remove the sensitive/large patient list.
    info = clinic_data.copy()
    info.pop('patients', None) # Remove 'patients' key safely
    
    return info

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s")
log = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI()

# Define the origins that are allowed to make requests
# In this case, just your local development machine
origins = [
    "http://127.0.0.1:8080",
    "http://localhost:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"], # Allow all methods (GET, POST, etc.)
    allow_headers=["*"], # Allow all headers
)

# --- Configuration Loading ---
# ... (HUME_API_KEY, TWILIO_ACCOUNT_SID, etc. all remain the same) ...
HUME_API_KEY = os.environ.get("HUME_API_KEY")
HUME_EVI_WS_URI = "wss://api.hume.ai/v0/evi/chat"
HUME_CONFIG_ID = os.environ.get("HUME_CONFIG_ID")
RENDER_APP_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")

# --- NEW: Firebase Initialization ---
try:
    # We don't need to pass any credentials! The SDK handles it.
    firebase_admin.initialize_app() 
    db = firestore.client()
    logging.info("Firebase Firestore client initialized successfully via GOOGLE_APPLICATION_CREDENTIALS.")
except Exception as e:
    # If it fails, log the real error.
    logging.error(f"Failed to initialize Firebase. Is GOOGLE_APPLICATION_CREDENTIALS set correctly? Error: {e}")
    db = None
# ------------------------------------

# --- Twilio Client Initialization ---
twilio_client = None
if all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN]):
    try:
        twilio_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        log.info("Twilio REST Client initialized successfully.")
    except Exception as e:
        log.error(f"Failed to initialize Twilio REST Client: {e}", exc_info=True)
# ... (rest of config checks) ...


# --- For Render - Wake Client ---
@app.get("/wake-up")
async def wake_up():
    """
    A simple GET endpoint that Render's free tier can hit
    to wake up the server from a cold start.
    """
    return JSONResponse(content={"status": "awake"})


@app.on_event("startup")
async def startup_event():
    """On startup, check if dummy patient exists in Firestore, if not, create it."""
    if not db:
        log.error("Firestore DB not available, skipping dummy patient creation.")
        return
        
    patient_mrn = "89728342"
    patient_ref = db.collection("patients").document(patient_mrn)
    
    if not patient_ref.get().exists:
        log.info(f"Dummy patient {patient_mrn} not found in Firestore. Creating...")
        dummy_data = {
            "phone_number": "+19087839700",
            "name": "Lon Kai",
            "date_of_birth": "03/21/1980",
            "age": 45,
            "gender": "Male",
            "medical_conditions": ["Hypertension", "Diabetes", "Hyperlipidemia"],
            "current_medications": ["Lisinopril 10 mg", "Metformin 500 mg", "Atorvastatin 20 mg"],
            "most_recent_visit": "2 weeks ago",
            "purpose_of_call": "post-visit follow up",
            "next_ai_call": "2025-10-30" # Example field for your cron job
        }
        patient_ref.set(dummy_data)
        log.info(f"Dummy patient {patient_mrn} created in Firestore.")
    else:
        log.info(f"Dummy patient {patient_mrn} already exists in Firestore.")


# --- UPDATED Patient Lookup Functions ---
def get_patient_info_by_mrn(mrn: str) -> dict | None:
    """Looks up patient data directly from Firestore by their MRN."""
    if not db:
        log.error("get_patient_info_by_mrn: Firestore client not available.")
        return None
    if not mrn:
        return None
    
    try:
        doc_ref = db.collection("patients").document(mrn)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            data["mrn"] = doc.id # Add the MRN (document ID) to the dict
            return data
        else:
            log.warning(f"Patient with MRN {mrn} not found in Firestore.")
            return None
    except Exception as e:
        log.error(f"Error getting patient {mrn} from Firestore: {e}", exc_info=True)
        return None
# ---------------------------------------

# --- WebSocket Connection Management ---
active_connections = {} 

# --- Helper Function for Cleanup ---
async def cleanup_connection(call_sid: str, reason: str = "Unknown"):
    # ... (This function remains exactly the same) ...
    log.info(f"Cleaning up connections for CallSid: {call_sid} (Reason: {reason})")
    connection_details = active_connections.pop(call_sid, None)
    if connection_details:
        hume_ws = connection_details.get("hume_ws")
        twilio_ws = connection_details.get("twilio_ws")
        if hume_ws and hume_ws.state != websockets.protocol.State.CLOSED:
            try: await hume_ws.close(code=1000, reason=f"Cleanup: {reason}")
            except Exception: pass
        if twilio_ws and twilio_ws.client_state != websockets.protocol.State.CLOSED:
             try: await twilio_ws.close(code=1000, reason=f"Cleanup: {reason}")
             except Exception: pass
    else:
        log.warning(f"Cleanup called for {call_sid}, but no active connection found.")


# --- Core API Endpoints ---
@app.get("/")
async def root():
    return {"message": "Healthcare AI Server (FastAPI) is running!"}

# --- Endpoint to Initiate Outbound Call ---
class StartCallRequest(BaseModel):
    mrn: str

# --- Start Call ---
@app.post("/api/start_call")
async def start_outbound_call(call_request: StartCallRequest):
    mrn = call_request.mrn
    log.info(f"Received request to call patient with MRN: {mrn}")

    if not twilio_client:
        log.error("Cannot place call: Twilio client is not initialized.")
        raise HTTPException(status_code=503, detail="Twilio client not available.")

    # --- Uses new Firestore lookup function ---
    patient_data = get_patient_info_by_mrn(mrn)
    if not patient_data:
        log.error(f"Cannot place call: MRN {mrn} not found in database.")
        raise HTTPException(status_code=404, detail="Patient MRN not found.")
    
    patient_number = patient_data.get("phone_number")
    if not patient_number:
        log.error(f"Cannot place call: Patient {mrn} has no phone number in record.")
        raise HTTPException(status_code=400, detail="Patient record missing phone number.")

    try:
        webhook_url = f"https://{RENDER_APP_HOSTNAME}/twilio/incoming_call?mrn={mrn}"
        log.info(f"Initiating outbound call via Twilio to {patient_number} (MRN {mrn})")
        log.info(f"Twilio will POST to webhook on answer: {webhook_url}")

        call = twilio_client.calls.create(
            to=patient_number,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url
        )
        
        log.info(f"Call initiated successfully via Twilio. New Call SID: {call.sid}")
        return JSONResponse(
            status_code=200,
            content={
                "message": "Call initiated successfully.",
                "patient_called": patient_number,
                "call_sid": call.sid,
                "mrn_sent": mrn
            }
        )
    except TwilioRestException as e:
        log.error(f"Twilio API error initiating call to {patient_number}: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Twilio API error: {e}")
    except Exception as e:
        log.error(f"Unexpected error initiating outbound call to {patient_number}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error initiating call.")

def generate_system_prompt(patient_data, clinic_name):
    """
    Generates a dynamic system prompt for the Hume EVI based on the patient's
    scheduled encounters and medical data.
    """
    
    # Default prompt if no specific action is found
    prompt = f"You are a friendly and caring AI medical assistant from {clinic_name}. You are calling {patient_data['name']}. Start by introducing yourself and asking how they are doing today."
    
    # Look for a scheduled AI phone call
    scheduled_ai_call = None
    if 'encounters' in patient_data and patient_data['encounters']:
        for date, encounter in patient_data['encounters'].items():
            if (encounter.get('status') == 'scheduled' and 
                encounter.get('type') == 'AI phone call'):
                scheduled_ai_call = encounter
                break # Found the first scheduled call

    if scheduled_ai_call:
        purpose = scheduled_ai_call.get('purpose', '').lower()
        patient_name = patient_data.get('name', 'the patient')
        
        if "medication adherence" in purpose:
            meds = ", ".join(patient_data.get('medications', []))
            if not meds:
                meds = "your new medications"
            prompt = (
                f"You are a friendly, caring AI medical assistant from {clinic_name}. "
                f"You are calling {patient_name} for a medication adherence check-in. "
                f"Your goal is to: "
                f"1. Introduce yourself and confirm you're speaking with {patient_name}. "
                f"2. Gently ask if they have been able to take their medications, specifically {meds}, as prescribed. "
                f"3. Ask if they are experiencing any side effects or have any questions about them. "
                f"4. If they have questions you can't answer, tell them you'll note it for their doctor. "
                f"5. Conclude the call warmly."
            )

        elif "post-op check-in" in purpose:
            procedures = ", ".join(patient_data.get('procedures_history', []))
            if not procedures:
                procedures = "your recent procedure"
            prompt = (
                f"You are a caring AI nurse from {clinic_name}. "
                f"You are calling {patient_name} for a post-operative check-in regarding their {procedures}. "
                f"Your goal is to: "
                f"1. Introduce yourself and confirm you're speaking with {patient_name}. "
                f"2. Ask how they are feeling and recovering. "
                f"3. Specifically ask about their pain level (e.g., on a scale of 1-10). "
                f"4. Ask if they've noticed any signs of infection, like fever or redness at the incision site. "
                f"5. Remind them of any key post-op instructions if you have them. "
                f"6. Conclude the call, advising them to call the office if their symptoms worsen."
            )
        
        elif "appointment reminder" in purpose:
            # Note: This would be even better if we found the *next* scheduled appt
            prompt = (
                f"You are a helpful AI administrative assistant from {clinic_name}. "
                f"You are calling {patient_name} to remind them of an upcoming appointment. "
                f"Your goal is to: "
                f"1. Introduce yourself and confirm you're speaking with {patient_name}. "
                f"2. Remind them of their upcoming appointment (you can ask them to confirm the date and time if it's not specified). "
                f"3. Ask if they have any questions before their visit. "
                f"4. Conclude the call."
            )
            
        else:
            # Fallback for other scheduled AI calls
            prompt = (
                f"You are a friendly AI medical assistant from {clinic_name}. "
                f"You are calling {patient_name} for a scheduled health check-in. "
                f"The purpose of the call is: {scheduled_ai_call.get('purpose', 'a general check-in')}. "
                f"Please begin the conversation, introduce yourself, and ask how they are doing."
            )

    logging.info(f"Generated system prompt: {prompt[:100]}...") # Log first 100 chars
    return prompt


async def save_call_results_to_firestore(patient_data, call_sid, transcript):
    """
    Saves the call results back to the patient's record in Firestore.
    Finds the first "scheduled" AI phone call and updates it.
    """
    try:
        logging.info(f"Attempting to save call results for CallSid: {call_sid}")
        
        # Find the first "scheduled" AI phone call to update
        encounter_to_update_date = None
        if 'encounters' in patient_data and patient_data['encounters']:
            scheduled_calls = []
            for date_str, encounter in patient_data['encounters'].items():
                if (encounter.get('status') == 'scheduled' and 
                    encounter.get('type') == 'AI phone call'):
                    scheduled_calls.append(date_str)
            
            if scheduled_calls:
                scheduled_calls.sort() # Sort by date to find the earliest one
                encounter_to_update_date = scheduled_calls[0]
        
        if not encounter_to_update_date:
            logging.warning(f"No scheduled AI phone call found for MRN {patient_data['mrn']}. Creating new encounter.")
            # If no scheduled call, create a new encounter entry
            encounter_to_update_date = datetime.now().strftime("%Y-%m-%d-%H%M%S") # Unique key
            base_encounter = {
                "type": "AI phone call (unscheduled)",
                "purpose": "General check-in"
            }
        else:
            base_encounter = patient_data['encounters'][encounter_to_update_date]

        # Prepare the update data
        # We use "dot notation" to update nested fields in a Firestore document
        update_path_prefix = f"encounters.{encounter_to_update_date}"
        update_data = {
            f"{update_path_prefix}.status": "completed",
            f"{update_path_prefix}.call_sid": call_sid,
            f"{update_path_prefix}.call_transcript": json.dumps(transcript), # Store transcript as a JSON string
            f"{update_path_prefix}.call_completed_at": datetime.now().isoformat()
        }

        # Get the document reference and apply the update
        patient_ref = db.collection('clinics').document(patient_data['clinic_id']).collection('patients').document(patient_data['mrn'])
        await asyncio.to_thread(patient_ref.update, update_data) # Run blocking DB call in a thread

        logging.info(f"Successfully saved call results for MRN {patient_data['mrn']} to encounter {encounter_to_update_date}")

    except Exception as e:
        logging.error(f"Error in save_call_results_to_firestore for CallSid {call_sid}: {e}", exc_info=True)


@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request, CallSid: str = Form(None)):
    """
    Handles incoming Twilio calls (both direct calls and click-to-call transfers).
    This function now generates a dynamic system prompt for Hume EVI.
    """
    logging.info(f"Twilio Call Webhook Received (CallSid: {CallSid})")
    
    try:
        # Check if this call is from our web app (click-to-call)
        # We get the MRN and ClinicID from the query params in the URL
        mrn = request.query_params.get('mrn')
        clinic_id = request.query_params.get('clinic_id')

        if not CallSid:
            logging.warning("Request missing CallSid.")
            CallSid = "UnknownCallSid" # Create a placeholder for logging

        if not mrn or not clinic_id:
            logging.error(f"Missing CallSid, MRN, or ClinicID in request. CallSid: {CallSid}, MRN: {mrn}, ClinicID: {clinic_id}")
            # This handles direct, unsolicited calls to the Twilio number
            # We can't look up a patient, so we give a generic response.
            response = VoiceResponse()
            response.say("Thank you for calling. This is an automated health service. We are unable to identify your patient record. Please contact your clinic directly. Goodbye.")
            response.hangup()
            return PlainTextResponse(str(response), media_type='application/xml')

        logging.info(f"Looking up patient for MRN: {mrn} in Clinic: {clinic_id}")

        # Get patient data from Firestore
        patient_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)
        patient_doc = patient_ref.get()

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
        clinic_info = db.collection('clinics').document(clinic_id).get()
        clinic_name = clinic_info.to_dict().get('name', 'your clinic')

        # --- NEW LOGIC ---
        # Generate a dynamic system prompt based on the patient's data
        system_prompt = generate_system_prompt(patient_data, clinic_name)
        # --- END NEW LOGIC ---

        # Start the WebSocket stream to Hume EVI
        try:
            logging.info(f"Connecting to Hume EVI for CallSid: {CallSid}")
            
            # Use the new dynamic system_prompt
            config = LanguageConfig(system_prompt=system_prompt)
            client = HumeStreamClient(os.environ.get("HUME_API_KEY"), config=config)
            
            # Start the EVI conversation
            # Note: We are not awaiting this, it runs in the background
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
        start.stream(url=f"wss://{request.headers['host']}/ws/{CallSid}")
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

async def start_hume_evi_conversation(client: HumeStreamClient, call_sid: str, patient_data: dict):
    """
    Handles the Hume EVI conversation for a given CallSid.
    Now captures the full transcript and saves it on disconnect.
    """
    full_transcript = [] # <-- NEW: List to store conversation
    
    try:
        async with client.connect(send_json=True) as socket:
            logging.info(f"Hume EVI WebSocket connected for CallSid: {call_sid}")
            
            # 1. Wait for the WebSocket connection from Twilio
            websocket = active_connections.get(call_sid)
            if not websocket:
                logging.error(f"Twilio WebSocket not found in active_connections for CallSid: {call_sid}")
                return

            logging.info(f"Twilio WebSocket connected for CallSid: {call_sid}")

            async def twilio_receiver():
                """Receives audio from Twilio and sends it to Hume."""
                while True:
                    try:
                        message = await websocket.receive_text()
                        data = json.loads(message)
                        
                        if data['event'] == 'media':
                            media = data['media']
                            chunk = media['payload']
                            await socket.send_json({
                                "type": "audio_input",
                                "data": chunk,
                            })
                        elif data['event'] == 'stop':
                            logging.info(f"Twilio 'stop' message received for CallSid: {call_sid}")
                            break
                    except WebSocketDisconnect:
                        logging.info(f"Twilio WebSocket disconnected (receiver) for CallSid: {call_sid}")
                        break
                    except Exception as e:
                        logging.error(f"Error in twilio_receiver for CallSid {call_sid}: {e}", exc_info=True)
                        break

            async def hume_receiver():
                """Receives audio and messages from Hume and sends them to Twilio."""
                while True:
                    try:
                        hume_message = await socket.recv_json()
                        
                        if hume_message["type"] == "audio_output":
                            chunk = hume_message["data"]
                            # Send audio to Twilio
                            response = {
                                "event": "media",
                                "streamSid": call_sid,
                                "media": {
                                    "payload": chunk
                                }
                            }
                            await websocket.send_text(json.dumps(response))
                        
                        # --- NEW: Capture Transcript ---
                        elif hume_message["type"] == "user_input":
                            logging.info(f"Transcript (User): {hume_message['message']['content']}")
                            full_transcript.append({
                                "role": "user",
                                "content": hume_message['message']['content'],
                                "timestamp": datetime.now().isoformat()
                            })
                        elif hume_message["type"] == "assistant_message":
                            logging.info(f"Transcript (AI): {hume_message['message']['content']}")
                            full_transcript.append({
                                "role": "assistant",
                                "content": hume_message['message']['content'],
                                "timestamp": datetime.now().isoformat()
                            })
                        # --- END NEW ---
                            
                        elif hume_message["type"] == "error":
                            logging.error(f"Hume EVI Error: {hume_message['error']} | Code: {hume_message.get('code')}")
                        
                    except Exception as e:
                        logging.error(f"Error in hume_receiver for CallSid {call_sid}: {e}", exc_info=True)
                        break

            # Run both receivers concurrently
            await asyncio.gather(twilio_receiver(), hume_receiver())

    except WebSocketDisconnect:
        logging.info(f"Hume WebSocket disconnected for CallSid: {call_sid}")
    except Exception as e:
        logging.error(f"Error in start_hume_evi_conversation for CallSid {call_sid}: {e}", exc_info=True)
    finally:
        logging.info(f"Call finished. Cleaning up for CallSid: {call_sid}")
        # --- NEW: Save results to Firestore ---
        if full_transcript:
            await save_call_results_to_firestore(patient_data, call_sid, full_transcript)
        # --- END NEW ---
        
        # Mark the call as complete to Twilio
        websocket = active_connections.get(call_sid)
        if websocket and websocket.client_state == 1: # STATE_CONNECTED
            try:
                # Send Twilio a "clear" message to hang up
                stop_message = {
                    "event": "clear",
                    "streamSid": call_sid
                }
                await websocket.send_text(json.dumps(stop_message))
                await websocket.close()
            except Exception as e:
                logging.warning(f"Error closing Twilio websocket: {e}")
        
        cleanup_connection(call_sid, "Call finished normally")

# --- WebSocket Endpoint for Twilio Audio Stream ---
@app.websocket("/twilio/audiostream/{call_sid}")
async def handle_twilio_audio_stream(websocket: WebSocket, call_sid: str):
    # ... (This function remains exactly the same as the previous robust version) ...
    connection_details = active_connections.get(call_sid)
    if not connection_details or not connection_details.get("hume_ws"):
        log.error(f"Twilio WS connected, but no active Hume connection found for CallSid: {call_sid}. Closing immediately.")
        return

    try:
        await websocket.accept()
        log.info(f"Twilio WebSocket accepted for CallSid: {call_sid}")
        connection_details["twilio_ws"] = websocket
        hume_ws = connection_details["hume_ws"]

        while True:
            if hume_ws.closed:
                log.warning(f"Hume WS is closed, stopping Twilio receive loop. CallSid: {call_sid}")
                break
                
            try:
                 message_str = await websocket.receive_text()
            except WebSocketDisconnect as e:
                 log.warning(f"Twilio WebSocket disconnected unexpectedly ({e.code}: {e.reason}). CallSid: {call_sid}")
                 break 

            try:
                data = json.loads(message_str)
            except json.JSONDecodeError:
                log.warning(f"Could not decode JSON from Twilio: {message_str[:100]}... CallSid: {call_sid}")
                continue

            event = data.get("event")

            if event == "start":
                stream_sid = data.get("start", {}).get("streamSid")
                if stream_sid:
                    connection_details["stream_sid"] = stream_sid
                    log.info(f"Twilio 'start' message received, Stream SID: {stream_sid}. CallSid: {call_sid}")
                else:
                    log.warning(f"Twilio 'start' message missing streamSid. CallSid: {call_sid}")

            elif event == "media":
                payload = data.get("media", {}).get("payload")
                if not payload:
                    log.warning(f"Twilio 'media' event missing payload. CallSid: {call_sid}")
                    continue

                if hume_ws.closed:
                    log.warning(f"Skipping Twilio media - Hume WS closed. CallSid: {call_sid}")
                    continue

                try:
                    mulaw_bytes = base64.b64decode(payload)
                    pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
                    pcm_b64 = base64.b64encode(pcm_bytes).decode('utf-8')
                    hume_message = { "type": "audio_input", "data": pcm_b64 }
                    await hume_ws.send(json.dumps(hume_message))
                except audioop.error as e:
                    log.error(f"Audioop error during Twilio->Hume transcoding: {e}. CallSid: {call_sid}")
                except websockets.exceptions.ConnectionClosed:
                     log.warning(f"Hume WS closed while trying to send audio. CallSid: {call_sid}")
                     break 
                except Exception as e:
                    log.error(f"Error processing/sending Twilio media: {e}. CallSid: {call_sid}", exc_info=True)

            elif event == "stop":
                log.info(f"Twilio 'stop' message received. CallSid: {call_sid}")
                break 

            elif event != "connected" and event != "mark":
                 log.warning(f"Received unknown event type from Twilio: {event}. CallSid: {call_sid}")

    except WebSocketDisconnect as e:
        log.warning(f"Twilio WebSocket disconnected ({e.code}: {e.reason}). CallSid: {call_sid}")
    except Exception as e:
        log.error(f"Unexpected error in handle_twilio_audio_stream for CallSid {call_sid}: {e}", exc_info=True)
    finally:
        log.info(f"Exiting Twilio audio stream handler. CallSid: {call_sid}")
        await cleanup_connection(call_sid, "Twilio stream ended/disconnected")


# --- UPDATED Background Task to Listen to Hume (with Firestore) ---
@app.websocket("/listen_to_hume/{call_sid}")
async def listen_to_hume(call_sid: str):
    log.info(f"Started listening to Hume EVI for CallSid: {call_sid}")
    hume_ws = None
    transcript = []
    is_interrupted = False
    patient_mrn = None # <-- NEW: To store the MRN

    try:
        connection_details = active_connections.get(call_sid)
        if not connection_details or not connection_details.get("hume_ws") or connection_details["hume_ws"].closed:
            log.error(f"listen_to_hume: Hume WS not found or already closed for {call_sid} at start. Task exiting.")
            return

        hume_ws = connection_details["hume_ws"]
        transcript = connection_details.get("transcript", [])
        is_interrupted = connection_details.get("is_interrupted", False)
        patient_mrn = connection_details.get("patient_mrn") # <-- NEW: Get MRN

        if not patient_mrn:
             log.error(f"listen_to_hume: Missing patient_mrn in connection details. Cannot update database. CallSid: {call_sid}")
             # We can continue the call, but can't save the data
        if not db:
            log.error(f"listen_to_hume: Firestore client 'db' is not available. Cannot update database. CallSid: {call_sid}")


        async for message_str in hume_ws:
            connection_details = active_connections.get(call_sid)
            if not connection_details:
                 log.warning(f"Connection details for {call_sid} disappeared mid-loop. Exiting listener task.")
                 break
            is_interrupted = connection_details.get("is_interrupted", False)

            try:
                hume_data = json.loads(message_str)
            except json.JSONDecodeError:
                log.warning(f"Could not decode JSON from Hume: {message_str[:100]}... CallSid: {call_sid}")
                continue

            hume_type = hume_data.get("type")

            if hume_type != "audio_output" and not (hume_type == "user_message" and hume_data.get("message", {}).get("metadata", {}).get("interim")):
                log.info(f"Hume Event: {hume_type}. CallSid: {call_sid}")

            # --- Process Hume Message Types ---
            if hume_type == "audio_output":
                # ... (This entire audio processing block is identical to the previous version) ...
                if is_interrupted:
                    log.info(f"Skipping Hume audio_output due to interruption. CallSid: {call_sid}")
                    continue
                twilio_ws = connection_details.get("twilio_ws")
                stream_sid = connection_details.get("stream_sid")
                if twilio_ws and stream_sid and twilio_ws.client_state == websockets.protocol.State.OPEN:
                    try:
                        wav_b64 = hume_data.get("data")
                        if not wav_b64:
                             log.warning(f"Hume audio_output message missing data. CallSid: {call_sid}")
                             continue
                        wav_bytes = base64.b64decode(wav_b64)
                        pcm_bytes_hume = b''
                        input_rate_hume = 8000; samp_width_hume = 2; n_channels = 1
                        with io.BytesIO(wav_bytes) as wav_file_like:
                            try:
                                with wave.open(wav_file_like, 'rb') as wav_reader:
                                    n_channels = wav_reader.getnchannels()
                                    samp_width_hume = wav_reader.getsampwidth()
                                    input_rate_hume = wav_reader.getframerate()
                                    if n_channels != 1 or samp_width_hume != 2:
                                        log.warning(f"Unexpected WAV format from Hume: C={n_channels}, W={samp_width_hume}, R={input_rate_hume}. CallSid: {call_sid}")
                                    if samp_width_hume != 2: continue
                                    pcm_bytes_hume = wav_reader.readframes(wav_reader.getnframes())
                            except wave.Error as e:
                                 log.error(f"Error reading Hume WAV data: {e}. CallSid: {call_sid}")
                                 continue
                        if not pcm_bytes_hume:
                             log.warning(f"Empty PCM data after reading Hume WAV. CallSid: {call_sid}")
                             continue
                        output_rate_twilio = 8000
                        pcm_bytes_8k = pcm_bytes_hume
                        if input_rate_hume != output_rate_twilio:
                            resample_state = connection_details.get("resample_state")
                            try:
                                pcm_bytes_8k, resample_state = audioop.ratecv(pcm_bytes_hume, samp_width_hume, 1, input_rate_hume, output_rate_twilio, resample_state)
                                connection_details["resample_state"] = resample_state
                            except audioop.error as e:
                                 log.error(f"Audioop error during resampling: {e}. CallSid: {call_sid}")
                                 continue
                        try:
                             mulaw_bytes = audioop.lin2ulaw(pcm_bytes_8k, samp_width_hume)
                        except audioop.error as e:
                             log.error(f"Audioop error during lin2ulaw conversion: {e}. CallSid: {call_sid}")
                             continue
                        mulaw_b64 = base64.b64encode(mulaw_bytes).decode('utf-8')
                        twilio_media_message = {
                            "event": "media", "streamSid": stream_sid,
                            "media": { "payload": mulaw_b64 }
                        }
                        try:
                             await twilio_ws.send_text(json.dumps(twilio_media_message))
                        except websockets.exceptions.ConnectionClosed:
                             log.warning(f"Twilio WS closed while trying to send audio. CallSid: {call_sid}")
                             break
                    except base64.binascii.Error as e:
                        log.error(f"Base64 decode error for Hume audio: {e}. CallSid: {call_sid}")
                    except Exception as e:
                         log.error(f"Unexpected error processing Hume audio chunk: {e}. CallSid: {call_sid}", exc_info=True)
                else:
                    if not (twilio_ws and twilio_ws.client_state == websockets.protocol.State.OPEN):
                         log.warning(f"Hume sent audio, but Twilio WS not open/ready. CallSid: {call_sid}")
                    if not stream_sid:
                         log.warning(f"Hume sent audio, but stream_sid not yet received from Twilio. CallSid: {call_sid}")

            elif hume_type in ("user_message", "assistant_message"):
                # ... (This interruption/transcript block is identical to the previous version) ...
                role = hume_data.get("message", {}).get("role", "unknown")
                content = hume_data.get("message", {}).get("content", "")
                is_interim = hume_data.get("message", {}).get("metadata", {}).get("interim", False)
                if role == "user" and is_interim:
                    if connection_details and not is_interrupted:
                        log.info(f"Interim user_message detected - Setting interruption flag. CallSid: {call_sid}")
                        connection_details["is_interrupted"] = True
                        is_interrupted = True
                elif role == "user" and not is_interim:
                    if connection_details:
                         if is_interrupted: log.info(f"Final user_message received - Resetting interruption flag. CallSid: {call_sid}")
                         connection_details["is_interrupted"] = False
                         is_interrupted = False
                    transcript.append(f"USER: {content}")
                    log.info(f"Transcript part added: USER: {content[:30]}... CallSid: {call_sid}")
                elif role == "assistant":
                     if connection_details:
                         if is_interrupted: log.info(f"Assistant message received - Resetting interruption flag. CallSid: {call_sid}")
                         connection_details["is_interrupted"] = False
                         is_interrupted = False
                     transcript.append(f"ASSISTANT: {content}")
                     log.info(f"Transcript part added: ASSISTANT: {content[:30]}... CallSid: {call_sid}")

            elif hume_type == "user_interruption":
                log.warning(f"Explicit user_interruption event received - Setting interruption flag. CallSid: {call_sid}")
                if connection_details:
                    connection_details["is_interrupted"] = True
                    is_interrupted = True

            elif hume_type == "tool_call":
                tool_name = hume_data.get("tool_call", {}).get("name")
                tool_call_id = hume_data.get("tool_call", {}).get("tool_call_id")

                if tool_name == "end_call_triage" and tool_call_id:
                    log.info(f"Hume requested 'end_call_triage' tool. CallSid: {call_sid}")
                    if connection_details:
                        connection_details["is_interrupted"] = False
                        is_interrupted = False
                    try:
                        args_str = hume_data.get("tool_call", {}).get("parameters", "{}")
                        args = json.loads(args_str)
                        summary = args.get("summary", "N/A")
                        action_statement = args.get("action_statement", "N/A")
                        
                        log.info("--- 📞 FINAL CALL SUMMARY DATA ---")
                        log.info(f"  Call SID: {call_sid}")
                        log.info(f"  Transcript:\n{json.dumps(transcript, indent=2)}")
                        log.info(f"  Summary: {summary}")
                        log.info(f"  Action Statement: {action_statement}")
                        
                        # --- NEW: Save data to Firestore ---
                        if db and patient_mrn:
                            try:
                                # Create a new subcollection "call_logs" for this patient
                                # Use the CallSid as the document ID for the log
                                log_ref = db.collection("patients").document(patient_mrn).collection("call_logs").document(call_sid)
                                log_data = {
                                    "timestamp": firestore.SERVER_TIMESTAMP,
                                    "call_sid": call_sid,
                                    "full_transcript": "\n".join(transcript), # Store as a single string
                                    "summary": summary,
                                    "action_statement": action_statement,
                                    "call_purpose": connection_details.get("variables", {}).get("call_purpose", "N/A") # Get from variables
                                }
                                log_ref.set(log_data)
                                log.info(f"Successfully saved call log to Firestore for MRN {patient_mrn}, CallSid {call_sid}")
                                
                                # We can also update the main patient document
                                patient_ref = db.collection("patients").document(patient_mrn)
                                patient_ref.update({
                                    "last_call_summary": summary,
                                    "last_call_action": action_statement,
                                    "last_call_timestamp": firestore.SERVER_TIMESTAMP,
                                    "next_ai_call": "2025-10-31" # Example: schedule next call for tomorrow
                                })
                                log.info(f"Successfully updated patient record for MRN {patient_mrn}")
                                
                            except Exception as e:
                                log.error(f"Failed to save call log to Firestore for MRN {patient_mrn}: {e}", exc_info=True)
                        else:
                            log.error(f"Cannot save to Firestore: DB client not available or MRN missing. CallSid: {call_sid}")
                        # ---------------------------------

                        tool_response_message = {
                            "type": "tool_response",
                            "tool_call_id": tool_call_id,
                            "content": json.dumps({"status": "success", "message": "Call triage data logged."})
                        }
                        
                        try:
                             await hume_ws.send(json.dumps(tool_response_message))
                             log.info(f"Sent tool_response back to Hume. CallSid: {call_sid}")
                             log.info(f"Waiting for Hume's final response... CallSid: {call_sid}")
                        except websockets.exceptions.ConnectionClosed:
                             log.warning(f"Hume WS closed while trying to send tool_response. CallSid: {call_sid}")
                             break

                    except json.JSONDecodeError as e:
                        log.error(f"Error decoding tool call arguments: {e}. Raw: {args_str}. CallSid: {call_sid}")
                    except Exception as e:
                        log.error(f"Error processing tool call: {e}. CallSid: {call_sid}", exc_info=True)
                else:
                     log.warning(f"Received tool_call for unknown tool '{tool_name}' or missing tool_call_id. CallSid: {call_sid}")

            elif hume_type == "error":
                log.error(f"Hume EVI Error (Full Message): {hume_data}. CallSid: {call_sid}")
                if hume_data.get('code', '').startswith('E'):
                     log.warning(f"Closing connection {call_sid} due to Hume fatal error code {hume_data.get('code')}.")
                     break 

    except websockets.exceptions.ConnectionClosedOK:
        log.info(f"Hume WebSocket closed normally (OK). CallSid: {call_sid}")
    except websockets.exceptions.ConnectionClosedError as e:
        log.warning(f"Hume WebSocket closed with error ({e.code}: {e.reason}). CallSid: {call_sid}")
    except websockets.exceptions.ConnectionClosed as e:
         log.warning(f"Hume WebSocket connection closed unexpectedly ({e.code}: {e.reason}). CallSid: {call_sid}")
    except Exception as e:
        log.error(f"Unexpected error in listen_to_hume main loop for CallSid {call_sid}: {e}", exc_info=True)
    finally:
        log.info(f"Stopped listening to Hume EVI for {call_sid}. Triggering cleanup.")
        await cleanup_connection(call_sid, "Hume listener stopped")