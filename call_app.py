import os
import asyncio
import logging
import json
import base64
import redis  # Import the redis library
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# --- Hume Imports ---
from hume.client import AsyncHumeClient
from hume.empathic_voice.chat.socket_client import ChatConnectOptions, SubscribeEvent
from hume.core.api_error import ApiError

# --- Configuration & Initialization ---

# 1. Load Environment Variables
load_dotenv()

# 2. Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s,%(msecs)03d [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 3. Initialize Firebase
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
HUME_SECRET_KEY = os.getenv("HUME_SECRET_KEY")
if not all([HUME_API_KEY, HUME_SECRET_KEY]):
    logging.error("Hume AI credentials missing. Check environment variables.")
hume_client = AsyncHumeClient(api_key=HUME_API_KEY)

# 6. Initialize Redis Client & Hostname
REDIS_URL = os.getenv("REDIS_URL")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME") 

if not RENDER_EXTERNAL_HOSTNAME:
    logging.warning("RENDER_EXTERNAL_HOSTNAME not set. WebSocket URLs may be incorrect if behind a proxy.")

if not REDIS_URL:
    logging.error("REDIS_URL not found. App will fail in multi-worker environment.")
    # Fallback for local testing, but not production-safe
    active_connections_local = {} 
    redis_client = None
else:
    try:
        redis_client = redis.from_url(REDIS_URL)
        redis_client.ping()
        logging.info("Successfully connected to Redis.")
    except Exception as e:
        logging.error(f"Could not connect to Redis: {e}. App will fail in multi-worker environment.")
        redis_client = None
        active_connections_local = {} # Fallback

# 7. Initialize FastAPI App
app = FastAPI()

# 8. Add CORS Middleware
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


# --- Redis/Local Connection Management ---

def set_connection_details(call_sid, details):
    """Saves connection details to Redis (or local dict) with an expiration."""
    details_serializable = {
        "system_prompt": details.get("system_prompt"),
        "doc_ref_path": details.get("doc_ref").path, # Store path, not object
        "encounter_date": details.get("encounter_date")
    }
    details_json_string = json.dumps(details_serializable)
    
    if redis_client:
        try:
            redis_client.setex(call_sid, 3600, details_json_string.encode('utf-8'))
        except Exception as e:
            logging.error(f"Redis setex failed: {e}")
    else:
        active_connections_local[call_sid] = details_json_string # No expiration in local fallback

def get_connection_details(call_sid):
    """Retrieves connection details from Redis (or local dict)."""
    details_json_string = None
    if redis_client:
        try:
            details_bytes = redis_client.get(call_sid)
            if details_bytes:
                details_json_string = details_bytes.decode('utf-8')
        except Exception as e:
            logging.error(f"Redis get failed: {e}")
            return None
    else:
        details_json_string = active_connections_local.get(call_sid)
        
    if not details_json_string:
        # NOTE: This log line is critical
        logging.error(f"No connection details found for CallSid {call_sid} in Redis/cache.")
        return None
        
    try:
        details_serializable = json.loads(details_json_string)
        doc_ref = db.document(details_serializable["doc_ref_path"]) # Recreate doc_ref from path
        
        return {
            "system_prompt": details_serializable.get("system_prompt"),
            "doc_ref": doc_ref,
            "encounter_date": details_serializable.get("encounter_date")
        }
    except Exception as e:
        logging.error(f"Failed to deserialize connection details: {e}")
        return None

def del_connection_details(call_sid):
    """Deletes connection details from Redis (or local dict)."""
    if redis_client:
        try:
            redis_client.delete(call_sid)
        except Exception as e:
            logging.error(f"Redis delete failed: {e}")
    else:
        if call_sid in active_connections_local:
            del active_connections_local[call_sid]


# --- Helper Functions (Sync) ---

def get_patient_doc_ref(clinic_id, mrn):
    """Fetches a patient's Firestore document reference. (SYNC)"""
    if not db:
        logging.error("Firestore DB not available.")
        return None
    try:
        doc_ref = db.collection(f"clinics/{clinic_id}/patients").document(mrn)
        doc = doc_ref.get()
        if not doc.exists:
            logging.warning(f"Patient doc not found for clinic {clinic_id}, MRN {mrn}")
            return None
        return doc_ref
    except Exception as e:
        logging.error(f"Error fetching patient doc ref: {e}")
        return None

def fetch_prompts(prompt_ids):
    """Fetches a list of prompts from the prompt_library collection. (SYNC)"""
    if not db:
        logging.error("Firestore DB not available.")
        return {}
    
    prompt_data = {}
    try:
        for doc_id in prompt_ids:
            doc_ref = db.collection("prompt_library").document(doc_id)
            doc = doc_ref.get()
            if doc.exists:
                prompt_data[doc_id] = doc.to_dict().get("content", "")
            else:
                logging.warning(f"Prompt document not found: {doc_id}")
                prompt_data[doc_id] = ""
    except Exception as e:
        logging.error(f"Error fetching prompts: {e}")
    return prompt_data

def generate_system_prompt(base_prompt, patient_data, purpose):
    """Generates a dynamic system prompt based on call purpose and patient data. (SYNC)"""
    system_prompt = base_prompt.replace("[Patient Name]", patient_data.get("name", "the patient"))
    
    kb_doc_id = ""
    if purpose == "medication adherence" or purpose == "medication follow-up":
        kb_doc_id = "kb_medication_adherence"
    elif purpose == "post-op checkin":
        kb_doc_id = "kb_post_op_checkin"
    
    if kb_doc_id:
        try:
            kb_prompts = fetch_prompts([kb_doc_id])
            kb_content = kb_prompts.get(kb_doc_id)
            if kb_content:
                system_prompt += f"\n\n--- {kb_doc_id.replace('_', ' ').title()} Protocol ---\n{kb_content}"
        except Exception as e:
            logging.error(f"Failed to fetch KB prompt {kb_doc_id}: {e}")

    if purpose == "medication adherence" or purpose == "medication follow-up":
        meds = ", ".join(patient_data.get("medications", [])) or "your new medications"
        system_prompt = system_prompt.replace("[Medication List]", meds)
    
    if purpose == "post-op checkin":
        proc = ", ".join(patient_data.get("procedures_history", [])) or "your recent procedure"
        system_prompt = system_prompt.replace("[Procedure Name]", proc)
        
    logging.info(f"Generated system prompt for purpose: {purpose}")
    return system_prompt


def save_call_results_to_firestore(doc_ref, encounter_date, call_sid, transcript):
    """Saves the call results back to the patient's encounter in Firestore. (SYNC)"""
    if not db:
        logging.error("Firestore DB not available, cannot save call results.")
        return
        
    try:
        encounter_path = f"encounters.{encounter_date}"
        update_data = {
            f"{encounter_path}.status": "completed",
            f"{encounter_path}.call_sid": call_sid,
            f"{encounter_path}.call_transcript": "\n".join(transcript)
        }
        
        doc_ref.update(update_data)
        logging.info(f"Successfully saved call results for CallSid {call_sid} to encounter {encounter_date}")
        
    except Exception as e:
        logging.error(f"Error saving call results for CallSid {call_sid}: {e}")

# --- Core Application Logic (Refactored for new Hume EVI) ---

class EviHandler:
    """
    This class handles the bi-directional streaming between Twilio and Hume EVI.
    It's created for each call and manages the callbacks from the Hume WebSocket.
    """
    def __init__(self, twilio_ws: WebSocket, hume_socket, call_sid: str, transcript_list: list):
        self.twilio_ws = twilio_ws
        self.hume_socket = hume_socket
        self.call_sid = call_sid
        self.transcript = transcript_list
        logging.info(f"EviHandler initialized for {call_sid}")

    async def on_open(self):
        logging.info(f"Hume EVI WebSocket connected for CallSid: {self.call_sid}")

    async def on_close(self):
        logging.info(f"Hume EVI WebSocket closed for CallSid: {self.call_sid}")

    async def on_error(self, error: ApiError):
        logging.error(f"Hume EVI Error for {self.call_sid}: {error.message}")
        self.transcript.append(f"EVI ERROR: {error.message}")

    async def on_message(self, message: SubscribeEvent):
        """Handles messages received *from* Hume EVI."""
        try:
            if message.type == "user_message" and message.message.content:
                self.transcript.append(f"Patient: {message.message.content}")
            elif message.type == "assistant_message" and message.message.content:
                self.transcript.append(f"Assistant: {message.message.content}")
            
            elif message.type == "audio_output":
                # This is audio from Hume. Decode it and send it to Twilio.
                audio_b64 = message.data
                
                # Format for Twilio Media Stream
                response_json = {
                    "event": "media",
                    "streamSid": self.call_sid, 
                    "media": {
                        "payload": audio_b64
                    }
                }
                await self.twilio_ws.send_text(json.dumps(response_json))
            
            elif message.type == "error":
                logging.error(f"Hume EVI Message Error for {self.call_sid}: {message.message}")
                self.transcript.append(f"EVI Error: {message.message}")

        except Exception as e:
            logging.error(f"Error in on_message for {self.call_sid}: {e}", exc_info=True)

    async def handle_twilio_audio(self):
        """Task to forward audio *from* Twilio *to* Hume."""
        try:
            while True:
                message_str = await self.twilio_ws.receive_text()
                message_json = json.loads(message_str)
                
                if message_json['event'] == 'media':
                    # This is audio from Twilio. Send it to Hume.
                    payload_b64 = message_json['media']['payload']
                    audio_bytes = base64.b64decode(payload_b64)
                    await self.hume_socket.send_bytes(audio_bytes)
                elif message_json['event'] == 'stop':
                    logging.info(f"Received 'stop' message from Twilio for {self.call_sid}")
                    await self.hume_socket.close() # This will trigger on_close
                    break
        except WebSocketDisconnect:
            logging.info(f"Twilio WebSocket disconnected (media stream) for {self.call_sid}")
        except Exception as e:
            logging.error(f"Error in Twilio audio stream for {self.call_sid}: {e}", exc_info=True)
            await self.hume_socket.close() # Ensure Hume socket closes on error


@app.websocket("/twilio/media/{call_sid}")
async def twilio_media_websocket(websocket: WebSocket, call_sid: str):
    """Handles the bidirectional audio stream from Twilio."""
    # --- NEW DEBUGGING: Wrap entire function in a try...except block ---
    try:
        logging.info(f"DEBUG: WebSocket route hit for {call_sid}")
        await websocket.accept()
        logging.info(f"DEBUG: WebSocket connection accepted for {call_sid}")
        
        logging.info(f"DEBUG: Attempting to get connection details for {call_sid} from Redis...")
        connection_details = get_connection_details(call_sid)
        
        if not connection_details:
            logging.error(f"DEBUG: No active connection details found for CallSid {call_sid}. Closing WebSocket.")
            await websocket.close()
            return
        
        logging.info(f"DEBUG: Successfully retrieved connection details for {call_sid}")

        system_prompt = connection_details.get("system_prompt", "You are a helpful assistant.")
        doc_ref = connection_details.get("doc_ref")
        encounter_date = connection_details.get("encounter_date")
        transcript = []
        
        logging.info(f"DEBUG: Attempting to connect to Hume EVI for {call_sid}...")
        
        # Use the new ChatConnectOptions
        options = ChatConnectOptions(
            system_prompt=system_prompt,
            secret_key=HUME_SECRET_KEY # Use the secret for auth
        )
        
        # Use the new connect_with_callbacks method
        async with hume_client.empathic_voice.chat.connect_with_callbacks(
            options=options,
            on_open=None,    # We will assign these via the handler
            on_message=None,
            on_close=None,
            on_error=None
        ) as socket:
            
            logging.info(f"DEBUG: Hume EVI connection successful for {call_sid}")
            
            # Create the handler class to manage the connection state
            handler = EviHandler(websocket, socket, call_sid, transcript)
            
            # Assign the callback methods from our handler
            socket.on_open = handler.on_open
            socket.on_message = handler.on_message
            socket.on_close = handler.on_close
            socket.on_error = handler.on_error
            
            # This task listens to Twilio and forwards audio to Hume
            await handler.handle_twilio_audio()

    except Exception as e:
        # --- NEW DEBUGGING: This will catch *any* error during WebSocket setup ---
        logging.critical(f"CRITICAL WEBSOCKET ERROR for {call_sid}: {e}", exc_info=True)
    finally:
        logging.info(f"Cleaning up WebSocket for {call_sid}")
        # Save results to Firestore
        if 'doc_ref' in locals() and 'encounter_date' in locals() and 'transcript' in locals():
            if doc_ref and encounter_date and transcript:
                save_call_results_to_firestore(doc_ref, encounter_date, call_sid, transcript)
        
        # Mark the call as complete with Twilio (if not already done)
        try:
            call = twilio_client.calls(call_sid).fetch()
            if call.status in ['initiated', 'ringing', 'in-progress']:
                twilio_client.calls(call_sid).update(status='completed')
                logging.info(f"Twilio call {call_sid} marked as 'completed'.")
        except Exception as e:
            logging.error(f"Could not update Twilio call {call_sid} status: {e}")
            
        # Clean up global connection tracking
        del_connection_details(call_sid)
        logging.info(f"Removed {call_sid} from active_connections.")


@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request):
    """Main webhook to handle incoming Twilio calls."""
    logging.info("Twilio call webhook received.")
    
    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
        
        mrn = request.query_params.get("mrn")
        clinic_id = request.query_params.get("clinic_id")

        if not call_sid:
            logging.error("Request is missing CallSid.")
            return VoiceResponse().say("An application error occurred. Missing call identifier.")
        
        if not mrn or not clinic_id:
            logging.error(f"Missing MRN or Clinic ID in webhook URL. CallSid: {call_sid}, MRN: {mrn}")
            return VoiceResponse().say("An application error occurred. Could not retrieve patient records.")

        logging.info(f"Processing call for Clinic: {clinic_id}, MRN: {mrn}, CallSid: {call_sid}")

        doc_ref = get_patient_doc_ref(clinic_id, mrn)
        if not doc_ref:
            logging.error(f"Could not find patient doc ref for MRN {mrn}. CallSid: {call_sid}")
            return VoiceResponse().say("An application error occurred. Could not find patient records.")
        
        patient_data = doc_ref.get().to_dict()

        scheduled_call = None
        encounter_date = None
        if "encounters" in patient_data:
            sorted_encounters = sorted(patient_data["encounters"].items(), key=lambda item: item[0], reverse=True)
            for date, encounter in sorted_encounters:
                if encounter.get("status") == "scheduled" and "AI" in encounter.get("type", ""):
                    scheduled_call = encounter
                    encounter_date = date
                    break 

        if not scheduled_call:
            logging.error(f"No scheduled AI call found for MRN {mrn}. CallSid: {call_sid}")
            return VoiceResponse().say("Thank you for calling. No scheduled AI interactions found for your account. Goodbye.")

        call_purpose = scheduled_call.get("purpose", "a routine check-in")
        logging.info(f"Found scheduled call with purpose: {call_purpose}")

        base_prompts_data = fetch_prompts(['prompt_identity', 'prompt_rules'])
        base_prompt = f"{base_prompts_data.get('prompt_identity', '')}\n\n{base_prompts_data.get('prompt_rules', '')}"
        
        system_prompt = generate_system_prompt(base_prompt, patient_data, call_purpose)

        # Store connection details in Redis (or local dict)
        set_connection_details(call_sid, {
            "system_prompt": system_prompt,
            "doc_ref": doc_ref,
            "encounter_date": encounter_date
        })

        response = VoiceResponse()
        connect = Connect()
        # --- FIX: Use the reliable external hostname from env vars ---
        hostname = RENDER_EXTERNAL_HOSTNAME or request.url.hostname
        websocket_url = f"wss://{hostname}/twilio/media/{call_sid}"
        logging.info(f"Connecting to WebSocket: {websocket_url}") # <-- NEW DEBUG LOG
        connect.stream(url=websocket_url)
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

