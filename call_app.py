import os
import asyncio
import logging
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
import firebase_admin
from firebase_admin import credentials, firestore
from hume import HumeStreamClient
from hume.models import LanguageConfig # <-- THIS IS THE FIX
from dotenv import load_dotenv
import json

# --- Configuration & Initialization ---

# 1. Load Environment Variables
# This loads from your .env file locally, or from Render's environment variables
load_dotenv()

# 2. Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s,%(msecs)03d [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 3. Initialize Firebase
# This uses the official environment variable that Render is now providing
try:
    firebase_admin.initialize_app()
    db = firestore.client()
    logging.info("Firebase Firestore client initialized successfully via GOOGLE_APPLICATION_CREDENTIALS.")
except Exception as e:
    logging.error(f"Failed to initialize Firebase. Is GOOGLE_APPLICATION_CREDENTIALS set correctly? Error: {e}")
    db = None

# 4. Initialize Twilio Client
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
    logging.error("Twilio credentials missing. Check environment variables.")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# 5. Initialize Hume Client
HUME_API_KEY = os.getenv("HUME_API_KEY")
HUME_CLIENT_SECRET = os.getenv("HUME_CLIENT_SECRET")
if not all([HUME_API_KEY, HUME_CLIENT_SECRET]):
    logging.error("Hume AI credentials missing. Check environment variables.")

# 6. Initialize FastAPI App
app = FastAPI()

# 7. Add CORS Middleware
# This allows your local web app (http://127.0.0.1:8080) to talk to your Render app.
origins = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global dictionary to keep track of active connections
active_connections = {}

# --- Helper Functions ---

async def get_patient_doc_ref(clinic_id, mrn):
    """Fetches a patient's Firestore document reference."""
    if not db:
        logging.error("Firestore DB not available.")
        return None
    try:
        doc_ref = db.collection(f"clinics/{clinic_id}/patients").document(mrn)
        if not doc_ref.get().exists:
            logging.warning(f"Patient doc not found for clinic {clinic_id}, MRN {mrn}")
            return None
        return doc_ref
    except Exception as e:
        logging.error(f"Error fetching patient doc ref: {e}")
        return None

async def fetch_prompts(prompt_ids):
    """Fetches a list of prompts from the prompt_library collection."""
    if not db:
        logging.error("Firestore DB not available.")
        return {}
    
    prompt_data = {}
    try:
        for doc_id in prompt_ids:
            doc_ref = db.collection("prompt_library").document(doc_id)
            doc = await doc_ref.get()
            if doc.exists:
                prompt_data[doc_id] = doc.to_dict().get("content", "")
            else:
                logging.warning(f"Prompt document not found: {doc_id}")
                prompt_data[doc_id] = ""
    except Exception as e:
        logging.error(f"Error fetching prompts: {e}")
    return prompt_data

async def generate_system_prompt(base_prompt, patient_data, purpose):
    """Generates a dynamic system prompt based on call purpose and patient data."""
    
    # Inject patient name
    system_prompt = base_prompt.replace("[Patient Name]", patient_data.get("name", "the patient"))
    
    # Fetch and append knowledge base
    kb_doc_id = ""
    if purpose == "medication adherence":
        kb_doc_id = "kb_medication_adherence"
    elif purpose == "post-op checkin":
        kb_doc_id = "kb_post_op_checkin"
    
    if kb_doc_id:
        try:
            kb_prompts = await fetch_prompts([kb_doc_id])
            kb_content = kb_prompts.get(kb_doc_id)
            if kb_content:
                system_prompt += f"\n\n--- {kb_doc_id.replace('_', ' ').title()} Protocol ---\n{kb_content}"
        except Exception as e:
            logging.error(f"Failed to fetch KB prompt {kb_doc_id}: {e}")

    # Inject specific data
    if purpose == "medication adherence":
        meds = ", ".join(patient_data.get("medications", [])) or "your new medications"
        system_prompt = system_prompt.replace("[Medication List]", meds)
    
    if purpose == "post-op checkin":
        proc = ", ".join(patient_data.get("procedures_history", [])) or "your recent procedure"
        system_prompt = system_prompt.replace("[Procedure Name]", proc)
        
    logging.info(f"Generated system prompt for purpose: {purpose}")
    return system_prompt


async def save_call_results_to_firestore(doc_ref, encounter_date, call_sid, transcript):
    """Saves the call results back to the patient's encounter in Firestore."""
    if not db:
        logging.error("Firestore DB not available, cannot save call results.")
        return
        
    try:
        # Create the path to the specific encounter
        encounter_path = f"encounters.{encounter_date}"
        
        # Prepare the update data
        update_data = {
            f"{encounter_path}.status": "completed",
            f"{encounter_path}.call_sid": call_sid,
            f"{encounter_path}.call_transcript": "\n".join(transcript)
        }
        
        await doc_ref.update(update_data)
        logging.info(f"Successfully saved call results for CallSid {call_sid} to encounter {encounter_date}")
        
    except Exception as e:
        logging.error(f"Error saving call results for CallSid {call_sid}: {e}")

# --- Core Application Logic ---

@app.websocket("/twilio/media/{call_sid}")
async def twilio_media_websocket(websocket: WebSocket, call_sid: str):
    """Handles the bidirectional audio stream from Twilio."""
    await websocket.accept()
    logging.info(f"WebSocket connection established for CallSid: {call_sid}")
    
    if call_sid not in active_connections:
        logging.error(f"No active connection details found for CallSid {call_sid}. Closing WebSocket.")
        await websocket.close()
        return

    connection_details = active_connections[call_sid]
    system_prompt = connection_details.get("system_prompt", "You are a helpful assistant.")
    doc_ref = connection_details.get("doc_ref")
    encounter_date = connection_details.get("encounter_date")
    transcript = []
    
    try:
        async with HumeStreamClient(HUME_API_KEY, HUME_CLIENT_SECRET) as client:
            config = LanguageConfig(system_prompt=system_prompt)
            async with client.connect_with_handlers(config=config) as socket:
                logging.info(f"Hume EVI WebSocket connected for CallSid: {call_sid}")

                async def handle_audio_stream(ws):
                    """Task to forward audio from Twilio to Hume."""
                    try:
                        while True:
                            message_str = await ws.receive_text()
                            message_json = json.loads(message_str)
                            
                            if message_json['event'] == 'media':
                                payload_b64 = message_json['media']['payload']
                                await socket.send_bytes(payload_b64)
                            elif message_json['event'] == 'stop':
                                logging.info(f"Received 'stop' message from Twilio for {call_sid}")
                                await socket.close()
                                break
                    except WebSocketDisconnect:
                        logging.info(f"Twilio WebSocket disconnected (media stream) for {call_sid}")
                    except Exception as e:
                        logging.error(f"Error in Twilio audio stream for {call_sid}: {e}")

                async def handle_evi_stream(ws):
                    """Task to forward audio from Hume to Twilio."""
                    try:
                        async for message in socket:
                            if message.type == "user_message" and message.message.content:
                                transcript.append(f"Patient: {message.message.content}")
                            elif message.type == "assistant_message" and message.message.content:
                                transcript.append(f"Assistant: {message.message.content}")
                            
                            if message.type == "audio_output":
                                audio_b64 = message.data
                                # Format for Twilio Media Stream
                                response_json = {
                                    "event": "media",
                                    "streamSid": call_sid, 
                                    "media": {
                                        "payload": audio_b64
                                    }
                                }
                                await ws.send_text(json.dumps(response_json))
                    except WebSocketDisconnect:
                        logging.info(f"Hume EVI WebSocket disconnected for {call_sid}")
                    except Exception as e:
                        logging.error(f"Error in Hume EVI stream for {call_sid}: {e}")

                # Run both tasks concurrently
                await asyncio.gather(handle_audio_stream(websocket), handle_evi_stream(websocket))

    except Exception as e:
        logging.error(f"WebSocket handling failed for {call_sid}: {e}")
    finally:
        logging.info(f"Cleaning up WebSocket for {call_sid}")
        # Save results to Firestore
        if doc_ref and encounter_date and transcript:
            await save_call_results_to_firestore(doc_ref, encounter_date, call_sid, transcript)
        
        # Mark the call as complete with Twilio (if not already done)
        try:
            call = twilio_client.calls(call_sid).fetch()
            if call.status in ['initiated', 'ringing', 'in-progress']:
                twilio_client.calls(call_sid).update(status='completed')
                logging.info(f"Twilio call {call_sid} marked as 'completed'.")
        except Exception as e:
            logging.error(f"Could not update Twilio call {call_sid} status: {e}")
            
        # Clean up global connection tracking
        if call_sid in active_connections:
            del active_connections[call_sid]
            logging.info(f"Removed {call_sid} from active_connections.")


@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request):
    """Main webhook to handle incoming Twilio calls."""
    logging.info("Twilio call webhook received.")
    
    try:
        # Parse data from Twilio (from /call route or incoming)
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
        
        # Data passed from our /call endpoint
        mrn = request.query_params.get("mrn")
        clinic_id = request.query_params.get("clinic_id")

        if not call_sid:
            logging.error("Request is missing CallSid.")
            return VoiceResponse().say("An application error occurred. Missing call identifier.")
        
        if not mrn or not clinic_id:
            logging.error(f"Missing MRN or Clinic ID in webhook URL. CallSid: {call_sid}, MRN: {mrn}")
            return VoiceResponse().say("An application error occurred. Could not retrieve patient records.")

        logging.info(f"Processing call for Clinic: {clinic_id}, MRN: {mrn}, CallSid: {call_sid}")

        # 1. Fetch patient data and doc ref
        doc_ref = await get_patient_doc_ref(clinic_id, mrn)
        if not doc_ref:
            logging.error(f"Could not find patient doc ref for MRN {mrn}. CallSid: {call_sid}")
            return VoiceResponse().say("An application error occurred. Could not find patient records.")
        
        patient_data = (await doc_ref.get()).to_dict()

        # 2. Find the scheduled AI call to get its purpose
        scheduled_call = None
        encounter_date = None
        if "encounters" in patient_data:
            for date, encounter in patient_data["encounters"].items():
                if encounter.get("status") == "scheduled" and "AI" in encounter.get("type", ""):
                    scheduled_call = encounter
                    encounter_date = date
                    break # Found the first scheduled AI call

        if not scheduled_call:
            logging.error(f"No scheduled AI call found for MRN {mrn}. CallSid: {call_sid}")
            return VoiceResponse().say("Thank you for calling. No scheduled AI interactions found for your account. Goodbye.")

        call_purpose = scheduled_call.get("purpose", "a routine check-in")
        logging.info(f"Found scheduled call with purpose: {call_purpose}")

        # 3. Generate the dynamic system prompt
        base_prompts_data = await fetch_prompts(['prompt_identity', 'prompt_rules'])
        base_prompt = f"{base_prompts_data.get('prompt_identity', '')}\n\n{base_prompts_data.get('prompt_rules', '')}"
        
        system_prompt = await generate_system_prompt(base_prompt, patient_data, call_purpose)

        # 4. Store connection details for the WebSocket
        active_connections[call_sid] = {
            "system_prompt": system_prompt,
            "doc_ref": doc_ref,
            "encounter_date": encounter_date
        }

        # 5. Respond to Twilio with TwiML
        response = VoiceResponse()
        connect = Connect()
        connect.stream(url=f"wss://{request.url.hostname}/twilio/media/{call_sid}")
        response.append(connect)
        response.say("Please wait while we connect you to our AI assistant.")
        
        logging.info(f"Returning TwiML to Twilio for CallSid: {call_sid}")
        return response.to_xml(), {"Content-Type": "text/xml"}

    except Exception as e:
        logging.error(f"Unexpected error in handle_incoming_call: {e}", exc_info=True)
        return VoiceResponse().say("An application error occurred. Please try again later.")

@app.get("/wake-up")
def wake_up():
    """Simple GET route to wake up the Render service."""
    logging.info("'/wake-up' endpoint was pinged.")
    return {"status": "awake"}