import os
import asyncio
import logging
import json
import base64
import redis  # Import the redis library
import audioop # Import for audio conversion
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Response
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# --- Hume Imports ---
from hume.client import AsyncHumeClient
from hume.empathic_voice.chat.socket_client import SubscribeEvent, AsyncChatSocketClient
from hume.core.api_error import ApiError
# --- CRITICAL FIX: Import the correct types ---
from hume.empathic_voice.types import AudioInput, ConnectSessionSettings 

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
        
        # --- NEW SCHEMA ---
        # We will add a placeholder summary. A separate function will be needed to do real summarization.
        summary = "Call completed. Summary pending generation."
        
        update_data = {
            f"{encounter_path}.status": "completed",
            f"{encounter_path}.call_sid": call_sid,
            f"{encounter_path}.call_transcript": "\n".join(transcript),
            f"{encounter_path}.details": summary, # <-- NEW FIELD
            f"{encounter_path}.encounter_id": f"ai-{call_sid}" # <-- NEW FIELD
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
    def __init__(self, twilio_ws: WebSocket, hume_socket: AsyncChatSocketClient, call_sid: str, transcript_list: list):
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
                # --- AUDIO FIX: Convert Hume's PCM audio back to Twilio's mu-law ---
                # 1. Get base64 PCM audio from Hume
                pcm_b64 = message.data
                
                # 2. Decode it into raw PCM-16 bytes
                pcm_bytes = base64.b64decode(pcm_b64)
                
                # 3. Convert PCM-16 bytes back to mu-law bytes
                # '2' is the width (2 bytes for 16-bit)
                mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)
                
                # 4. Re-encode the new mu-law bytes into base64
                mulaw_b64 = base64.b64encode(mulaw_bytes).decode('utf-8')
                
                # 5. Format for Twilio Media Stream
                response_json = {
                    "event": "media",
                    "streamSid": self.call_sid, 
                    "media": {
                        "payload": mulaw_b64 # <-- Send corrected audio format
                    }
                }
                await self.twilio_ws.send_text(json.dumps(response_json))
            
            elif message.type == "user_interruption":
                logging.info(f"Hume detected user interruption for {self.call_sid}. Clearing Twilio audio queue.")
                # Send a "clear" message to Twilio to stop any queued audio
                clear_message = {
                    "event": "clear",
                    "streamSid": self.call_sid
                }
                await self.twilio_ws.send_text(json.dumps(clear_message))

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
                    # 1. Get base64 mu-law audio from Twilio
                    payload_b64 = message_json['media']['payload']
                    
                    # 2. Decode it into raw mu-law bytes
                    mulaw_bytes = base64.b64decode(payload_b64)
                    
                    # 3. Convert mu-law bytes to PCM-16 (bytes) for Hume
                    # '2' is the width (2 bytes for 16-bit)
                    pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
                    
                    # 4. Re-encode the new PCM-16 bytes into base64
                    pcm_b64 = base64.b64encode(pcm_bytes).decode('utf-8')
                    
                    # 5. --- CRITICAL FIX: Use send_publish (the new method)
                    #    and pass it the AudioInput *model* it expects.
                    audio_message = AudioInput(
                        type="audio_input",
                        data=pcm_b64
                        )
                    logging.info({audio_message})
                    await self.hume_socket.send_publish(audio_message)
                    
                elif message_json['event'] == 'stop':
                    logging.info(f"Received 'stop' message from Twilio for {self.call_sid}")
                    break
        except WebSocketDisconnect:
            logging.info(f"Twilio WebSocket disconnected (media stream) for {self.call_sid}")
        except Exception as e:
            logging.error(f"Error in Twilio audio stream for {self.call_sid}: {e}", exc_info=True)


@app.websocket("/twilio/media/{call_sid}")
async def twilio_media_websocket(websocket: WebSocket, call_sid: str):
    """Handles the bidirectional audio stream from Twilio."""
    transcript = [] # Define transcript here to be in scope for 'finally'
    doc_ref = None      # Define doc_ref here
    encounter_date = None # Define encounter_date here
    twilio_listener_task = None # Define task here
    
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

        doc_ref = connection_details.get("doc_ref") # Assign to outer scope variable
        encounter_date = connection_details.get("encounter_date") # Assign to outer scope variable
        
        logging.info(f"DEBUG: Attempting to connect to Hume EVI for {call_sid}...")
        
        # --- CRITICAL FIX: Create handler class *first* ---
        handler = EviHandler(websocket, None, call_sid, transcript) # Socket is None for now

        # --- CRITICAL FIX: Use .connect() and pass session_settings ---
        # 1. Create the new session settings object
        session_settings = ConnectSessionSettings(
            system_prompt=system_prompt
        )

        # 2. Use the new .connect() method and pass callbacks
        async with hume_client.empathic_voice.chat.connect(
            session_settings=session_settings,
        ) as socket:
            
            logging.info(f"DEBUG: Hume EVI connection successful for {call_sid}")
            
            # 3. Now that we have the socket, assign it to the handler
            handler.hume_socket = socket
            
            # 4. The Hume listener is already running (managed by connect_with_callbacks).
            #    We only need to create and await our Twilio listener.
            twilio_listener_task = asyncio.create_task(handler.handle_twilio_audio())
            
            await twilio_listener_task

    except Exception as e:
        logging.critical(f"CRITICAL WEBSOCKET ERROR for {call_sid}: {e}", exc_info=True)
    finally:
        logging.info(f"Cleaning up WebSocket for {call_sid}")
        # Cancel any lingering tasks
        if twilio_listener_task and not twilio_listener_task.done():
            twilio_listener_task.cancel()
            
        # Save results to Firestore
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
    response = VoiceResponse() # Create a base response
    
    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid")
        
        mrn = request.query_params.get("mrn")
        clinic_id = request.query_params.get("clinic_id")

        if not call_sid:
            logging.error("Request is missing CallSid.")
            response.say("An application error occurred. Missing call identifier.")
            return Response(content=response.to_xml(), media_type="text/xml")
        
        if not mrn or not clinic_id:
            logging.error(f"Missing MRN or Clinic ID in webhook URL. CallSid: {call_sid}, MRN: {mrn}")
            response.say("An application error occurred. Could not retrieve patient records.")
            return Response(content=response.to_xml(), media_type="text/xml")

        logging.info(f"Processing call for Clinic: {clinic_id}, MRN: {mrn}, CallSid: {call_sid}")

        doc_ref = get_patient_doc_ref(clinic_id, mrn)
        if not doc_ref:
            logging.error(f"Could not find patient doc ref for MRN {mrn}. CallSid: {call_sid}")
            return Response(content=response.to_xml(), media_type="text/xml")
        
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
            return Response(content=response.to_xml(), media_type="text/xml")

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

        # --- THIS IS THE "SUCCESS" PATH ---
        # --- TwiML FIX: Say *before* you Connect ---
        response.say("Please wait while we connect you to our AI assistant.")
        connect = Connect()
        hostname = RENDER_EXTERNAL_HOSTNAME or request.url.hostname
        websocket_url = f"wss://{hostname}/twilio/media/{call_sid}"
        logging.info(f"Connecting to WebSocket: {websocket_url}") 
        connect.stream(url=websocket_url)
        response.append(connect)
        
        logging.info(f"Returning TwiML to Twilio for CallSid: {call_sid}")
        return Response(content=response.to_xml(), media_type="text/xml")

    except Exception as e:
        logging.error(f"Unexpected error in handle_incoming_call: {e}", exc_info=True)
        response = VoiceResponse() # Create a fresh response for the error
        response.say("An application error occurred. Please try again later.")
        return Response(content=response.to_xml(), media_type="text/xml")

# --- FIX: Added root route to handle health checks and fix 404s ---
@app.get("/")
def root():
    """Root path for Render health checks."""
    return {"status": "ok", "message": "WellSaid AI Call App is running."}

@app.get("/wake-up")
def wake_up():
    """Simple GET route to wake up the Render service."""
    return {"status": "awake"}

