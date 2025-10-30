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
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.rest import Client as TwilioRestClient
from twilio.base.exceptions import TwilioRestException
from pydantic import BaseModel
import firebase_admin # <-- NEW
from firebase_admin import credentials, firestore 
from dotenv import load_dotenv
load_dotenv() # This line actually reads the .env file

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


# --- DUMMY_PATIENT_DB is now REMOVED ---
# We will create a function to add our dummy patient to Firestore if it doesn't exist

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

# --- Twilio Webhook (Handles Outbound-Answered Calls) ---
@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request):
    log.info("-" * 30)
    log.info("Twilio Call Webhook Received (Outbound-Answered)")
    call_sid = None 
    
    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid')
        from_number = form_data.get('From')
        mrn = request.query_params.get('mrn')

        if not call_sid or not mrn:
             log.error(f"Missing CallSid or MRN in request. CallSid: {call_sid}, MRN: {mrn}")
             response = VoiceResponse(); response.say("System configuration error."); response.hangup()
             return Response(content=str(response), media_type="text/xml", status_code=200)

        log.info(f"CallSid: {call_sid}, Patient Number: {from_number}, MRN from URL: {mrn}")

        # --- 1. Identify Patient (using Firestore MRN lookup) ---
        patient_data = get_patient_info_by_mrn(mrn)
        if not patient_data:
            log.error(f"Could not find patient for MRN {mrn} provided in webhook URL. CallSid: {call_sid}")
            response = VoiceResponse(); response.say("System error: Could not retrieve patient records.", voice='alice'); response.hangup()
            return Response(content=str(response), media_type="text/xml", status_code=200)

        log.info(f"Identified Patient: {patient_data.get('name', 'N/A')} (MRN: {mrn})")
        
        # Store the MRN in the active connection for the tool call later
        patient_mrn = patient_data.get("mrn") 

        # --- 2. Connect to Hume EVI ---
        if not all([HUME_API_KEY, HUME_EVI_WS_URI, HUME_CONFIG_ID]):
            log.error(f"Hume configuration missing. Cannot connect. CallSid: {call_sid}")
            response = VoiceResponse(); response.say("AI service configuration error."); response.hangup()
            return Response(content=str(response), media_type="text/xml", status_code=200)
            
        uri_with_key_and_config = (
            f"{HUME_EVI_WS_URI}?apiKey={HUME_API_KEY}"
            f"&config_id={HUME_CONFIG_ID}"
            f"&verbose_transcription=true"
        )
        log.info(f"Connecting to Hume EVI WebSocket... CallSid: {call_sid}")
        
        try:
             hume_websocket = await websockets.connect(uri_with_key_and_config)
        except websockets.exceptions.WebSocketException as e:
             log.error(f"Failed to connect to Hume EVI WebSocket: {e}. CallSid: {call_sid}", exc_info=True)
             response = VoiceResponse(); response.say("Could not connect to the AI service."); response.hangup()
             return Response(content=str(response), media_type="text/xml", status_code=200)

        log.info(f"WebSocket connection to Hume EVI established. CallSid: {call_sid}")

        active_connections[call_sid] = {
            "hume_ws": hume_websocket,
            "twilio_ws": None,
            "stream_sid": None,
            "resample_state": None,
            "transcript": [],
            "is_interrupted": False,
            "patient_mrn": patient_mrn # <-- Store MRN for use in listen_to_hume
        }

        # --- 3. Send Initial Settings (with VARIABLES and TOOL) ---
        conditions_list = ", ".join(patient_data.get('medical_conditions', ['N/A']))
        medications_list = ", ".join(patient_data.get('current_medications', ['N/A']))
        
        tool_params_schema = {
            "type": "object",
            "properties": {
                "summary": { "type": "string", "description": "Summary of patient concern and call outcome."},
                "action_statement": { "type": "string", "description": "Final action: EMERGENCY_911, REFILL_REQUEST, NOTE_FOR_REVIEW, or VERIFICATION_FAILED."}
            },
            "required": ["summary", "action_statement"]
        }

        initial_message = {
            "type": "session_settings",
            "variables": {
                "patient_name": patient_data.get('name', 'the patient'),
                "dob": patient_data.get('date_of_birth', 'N/A'),
                "age": str(patient_data.get('age', 'N/A')),
                "gender": patient_data.get('gender', 'N/A'),
                "conditions": conditions_list,
                "medications": medications_list,
                "last_visit": patient_data.get('most_recent_visit', 'N/A'),
                "call_purpose": patient_data.get('purpose_of_call', 'a routine check-in')
            },
            "audio": { "encoding": "linear16", "sample_rate": 8000, "channels": 1 },
            "voice": { "id": "97fe9008-8584-4d56-8453-bd8c7ead3663", "provider": "HUME_AI" },
            "evi_version": "3",
            "tools": [{
                "type": "function", "name": "end_call_triage",
                "description": "Summarize call and log final action.",
                "parameters": json.dumps(tool_params_schema)
            }]
        }
        
        try:
            await hume_websocket.send(json.dumps(initial_message))
            log.info(f"Sent session_settings to Hume EVI. CallSid: {call_sid}")
        except websockets.exceptions.ConnectionClosed as e:
             log.error(f"Hume WS closed unexpectedly after connect, before sending settings: {e}. CallSid: {call_sid}", exc_info=True)
             await cleanup_connection(call_sid, "Hume WS closed early")
             response = VoiceResponse(); response.say("AI connection lost early."); response.hangup()
             return Response(content=str(response), media_type="text/xml", status_code=200)

        asyncio.create_task(listen_to_hume(call_sid))

    except Exception as e:
        log.error(f"Unexpected error in handle_incoming_call for CallSid {call_sid}: {e}", exc_info=True)
        await cleanup_connection(call_sid, "Incoming call setup failed")
        response = VoiceResponse(); response.say("An unexpected server error occurred during setup."); response.hangup()
        return Response(content=str(response), media_type="text/xml", status_code=200)

    # --- 4. Respond to Twilio with TwiML to start <Stream> ---
    response = VoiceResponse()
    connect = Connect()
    stream_url = f"wss://{RENDER_APP_HOSTNAME}/twilio/audiostream/{call_sid}"
    log.info(f"Telling Twilio to stream audio to: {stream_url}. CallSid: {call_sid}")
    connect.stream(url=stream_url)
    response.append(connect)
    response.pause(length=120)
    log.info(f"Responding to Twilio with TwiML <Connect><Stream>. CallSid: {call_sid}")
    return Response(content=str(response), media_type="text/xml", status_code=200)


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