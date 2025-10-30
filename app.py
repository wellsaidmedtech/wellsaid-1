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
from pydantic import BaseModel

# --- Basic Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI()

# --- Configuration Loading ---
HUME_API_KEY = os.environ.get("HUME_API_KEY")
HUME_EVI_WS_URI = "wss://api.hume.ai/v0/evi/chat"
HUME_CONFIG_ID = os.environ.get("HUME_CONFIG_ID")
RENDER_APP_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")

# Check for essential configuration
if not all([HUME_API_KEY, HUME_CONFIG_ID, RENDER_APP_HOSTNAME, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
    log.error("FATAL: Missing one or more required environment variables.")

# Instantiate the Twilio client
try:
    twilio_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    log.info("--- Twilio REST Client initialized successfully. ---")
except Exception as e:
    log.error(f"--- FAILED to initialize Twilio REST Client: {e} ---")
    twilio_client = None

# --- Patient Database with MRN as Key ---
# --- UPDATED: Hard-coded MRN for testing ---
patient_mrn = "89728342" # Use a consistent MRN
# ----------------------------------------

DUMMY_PATIENT_DB = {
    patient_mrn: { # Key is the patient's MRN
        "phone_number": "+19087839700", # Phone number is now a field
        "name": "Lon Kai",
        "date_of_birth": "03/21/1980",
        "age": 45,
        "gender": "Male",
        "medical_conditions": ["Hypertension", "Diabetes", "Hyperlipidemia"],
        "current_medications": ["Lisinopril 10 mg", "Metformin 500 mg", "Atorvastatin 20 mg"],
        "most_recent_visit": "2 weeks ago",
        "purpose_of_call": "post-visit follow up"
    }
}
log.info(f"--- Loaded dummy patient 'Lon Kai' with MRN: {patient_mrn} ---")

# --- Patient Lookup Functions ---
def get_patient_info_by_phone(phone_number: str) -> dict | None:
    """Looks up patient data by *searching* for their phone number."""
    if not phone_number:
        return None
    for mrn, data in DUMMY_PATIENT_DB.items():
        if data.get("phone_number") == phone_number:
            return {"mrn": mrn, **data}
    return None

def get_patient_info_by_mrn(mrn: str) -> dict | None:
    """Looks up patient data directly by their MRN (the key)."""
    if not mrn:
        return None
    data = DUMMY_PATIENT_DB.get(mrn)
    if data:
        return {"mrn": mrn, **data}
    return None

# --- WebSocket Connection Management ---
active_connections = {} 

# --- Helper Function for Cleanup ---
async def cleanup_connection(call_sid: str, reason: str = "Unknown"):
    log.info(f"--- Cleaning up connections for CallSid: {call_sid} (Reason: {reason}) ---")
    connection_details = active_connections.pop(call_sid, None)
    if connection_details:
        hume_ws = connection_details.get("hume_ws")
        twilio_ws = connection_details.get("twilio_ws")
        if hume_ws and not hume_ws.closed:
            await hume_ws.close(code=1000, reason=f"Cleanup: {reason}")
        if twilio_ws and not twilio_ws.client_state == websockets.protocol.State.CLOSED:
             await twilio_ws.close(code=1000, reason=f"Cleanup: {reason}")
    else:
        log.warning(f"--- Cleanup called for {call_sid}, but no active connection found. ---")

# --- Core API Endpoints ---
@app.get("/")
async def root():
    return {"message": "Healthcare AI Server (FastAPI) is running!"}

# --- Endpoint to Initiate Outbound Call ---
# --- UPDATED: Pydantic model now expects 'mrn' ---
class StartCallRequest(BaseModel):
    mrn: str
# ---------------------------------------------

@app.post("/api/start_call")
async def start_outbound_call(call_request: StartCallRequest):
    """
    Triggers an outbound call to a patient using their MRN.
    """
    mrn = call_request.mrn
    log.info(f"--- Received request to call patient with MRN: {mrn} ---")

    if not twilio_client:
        log.error("--- Cannot place call: Twilio client is not initialized. ---")
        raise HTTPException(status_code=500, detail="Twilio client not initialized. Check server logs.")

    # --- UPDATED: Find patient by MRN ---
    patient_data = get_patient_info_by_mrn(mrn)
    if not patient_data:
        log.error(f"--- Cannot place call: MRN {mrn} not found in database. ---")
        raise HTTPException(status_code=404, detail="Patient MRN not found in database.")
    
    # --- UPDATED: Get phone number *from* patient data ---
    patient_number = patient_data.get("phone_number")
    if not patient_number:
        log.error(f"--- Cannot place call: Patient {mrn} has no phone number. ---")
        raise HTTPException(status_code=400, detail="Patient record has no phone number.")
    # ----------------------------------------------------

    try:
        # This part is correct: we pass the MRN to the webhook
        webhook_url = f"https://{RENDER_APP_HOSTNAME}/twilio/incoming_call?mrn={mrn}"
        
        log.info(f"--- Initiating outbound call via Twilio to {patient_number} (for MRN {mrn}) ---")
        log.info(f"--- Twilio will call this webhook on answer: {webhook_url} ---")

        call = twilio_client.calls.create(
            to=patient_number,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url
        )
        
        log.info(f"--- Call initiated successfully. New Call SID: {call.sid} ---")
        return JSONResponse(
            status_code=200,
            content={
                "message": "Call initiated successfully.",
                "patient_called": patient_number,
                "call_sid": call.sid,
                "mrn_sent": mrn
            }
        )
    except Exception as e:
        log.error(f"--- FAILED to initiate outbound call: {e} ---")
        raise HTTPException(status_code=500, detail=f"Twilio API error: {str(e)}")

# --- Twilio Webhook (Handles BOTH Inbound and Outbound-Answered Calls) ---
@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request):
    """
    This webhook is hit by Twilio when the outbound call is answered.
    It now *gets the patient's MRN from the URL*.
    """
    log.info("-" * 30)
    log.info(">>> Twilio Call Webhook Received (Inbound or Outbound-Answered) <<<")
    call_sid = None
    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid')
        from_number = form_data.get('From')
        
        # Get the MRN we passed in the URL
        mrn = request.query_params.get('mrn')

        if not call_sid or not mrn:
             log.error(f"--- ERROR: Missing CallSid or MRN in request. CallSid: {call_sid}, MRN: {mrn} ---")
             raise HTTPException(status_code=400, detail="Missing required call information")

        log.info(f"  Call SID: {call_sid}")
        log.info(f"  Call From (Patient Number): {from_number}")
        log.info(f"  Looking up patient by MRN: {mrn}")

        # --- 1. Identify Patient (using MRN) ---
        patient_data = get_patient_info_by_mrn(mrn)
        if not patient_data:
            log.error(f"--- CRITICAL ERROR: Could not find patient for MRN {mrn}. ---")
            response = VoiceResponse()
            response.say("Sorry, we had a system error and could not retrieve your records. Please call our main office.", voice='alice')
            response.hangup()
            return Response(content=str(response), media_type="text/xml")

        log.info(f"--- Identified Patient: {patient_data.get('name', 'N/A')} (MRN: {patient_data.get('mrn', 'N/A')}) ---")

        # --- 2. Connect to Hume EVI (with config_id in URL) ---
        uri_with_key_and_config = f"{HUME_EVI_WS_URI}?apiKey={HUME_API_KEY}&config_id={HUME_CONFIG_ID}"
        log.info(f"--- Connecting to WebSocket URL (with config_id): {HUME_EVI_WS_URI}?apiKey=[REDACTED]&config_id={HUME_CONFIG_ID} ---")
        
        hume_websocket = await websockets.connect(uri_with_key_and_config)
        log.info("--- WebSocket connection to Hume EVI established. ---")

        active_connections[call_sid] = {
            "hume_ws": hume_websocket,
            "twilio_ws": None,
            "stream_sid": None,
            "resample_state": None,
            "transcript": [] 
        }

        # --- 3. Send Initial Settings (with VARIABLES) ---
        conditions_list = ", ".join(patient_data.get('medical_conditions', ['N/A']))
        medications_list = ", ".join(patient_data.get('current_medications', ['N/A']))
        
        # Define the tool parameters schema as a Python dict first
        tool_params_schema = {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A one or two-sentence summary of the patient's concern and the call's outcome."
                },
                "action_statement": {
                    "type": "string",
                    "description": "The final categorized action. Must be one of: 'EMERGENCY_911', 'REFILL_REQUEST', 'NOTE_FOR_REVIEW', or 'VERIFICATION_FAILED'."
                }
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
            "audio": {
                "encoding": "linear16",
                "sample_rate": 8000,
                "channels": 1
            },
            "voice": {
                "id": "97fe9008-8584-4d56-8453-bd8c7ead3663",
                "provider": "HUME_AI"
            },
            "evi_version": "3",
            "tools": [
                {
                    "type": "function",
                    "name": "end_call_triage",
                    "description": "Call this tool at the end of the conversation to summarize the call and log the final action.",
                    # Stringify the parameters object
                    "parameters": json.dumps(tool_params_schema)
                }
            ]
        }
        await hume_websocket.send(json.dumps(initial_message))
        log.info("--- Sent session_settings (with variables and *stringified* tool params) to Hume EVI ---")

        asyncio.create_task(listen_to_hume(call_sid))

    except Exception as e:
        log.error(f"--- UNEXPECTED ERROR in handle_incoming_call for {call_sid}: {type(e).__name__} - {e} ---")
        await cleanup_connection(call_sid, "Incoming call setup failed")
        response = VoiceResponse(); response.say("An unexpected server error occurred."); response.hangup()
        return Response(content=str(response), media_type="text/xml")

    # --- 4. Respond to Twilio (This starts the <Stream>) ---
    response = VoiceResponse()
    connect = Connect()
    stream_url = f"wss://{RENDER_APP_HOSTNAME}/twilio/audiostream/{call_sid}"
    log.info(f"--- Telling Twilio to stream audio to: {stream_url} ---")
    connect.stream(url=stream_url)
    response.append(connect)
    response.pause(length=120)
    log.info("--- Responding to Twilio with TwiML <Connect><Stream> ---")
    return Response(content=str(response), media_type="text/xml")

# --- WebSocket Endpoint for Twilio Audio Stream ---
@app.websocket("/twilio/audiostream/{call_sid}")
async def handle_twilio_audio_stream(websocket: WebSocket, call_sid: str):
    # ... (This function remains exactly the same) ...
    try:
        await websocket.accept()
        log.info(f"--- Twilio WebSocket connected for CallSid: {call_sid} ---")

        connection_details = active_connections.get(call_sid)
        if not connection_details or not connection_details.get("hume_ws"):
            log.error(f"--- ERROR: Twilio WS connected, but Hume connection not found for CallSid: {call_sid}. Closing. ---")
            await websocket.close(code=1011, reason="Backend EVI connection missing")
            return

        connection_details["twilio_ws"] = websocket
        hume_ws = connection_details["hume_ws"]

        while True:
            message_str = await websocket.receive_text()
            data = json.loads(message_str)
            event = data.get("event")

            if event == "start":
                stream_sid = data["start"]["streamSid"]
                connection_details["stream_sid"] = stream_sid
                log.info(f"--- Twilio 'start' message received, Stream SID: {stream_sid} ---")

            elif event == "media":
                payload = data["media"]["payload"]
                if hume_ws.closed: continue

                try:
                    mulaw_bytes = base64.b64decode(payload)
                    pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2) 
                    pcm_b64 = base64.b64encode(pcm_bytes).decode('utf-8')
                    hume_message = { "type": "audio_input", "data": pcm_b64 }
                    await hume_ws.send(json.dumps(hume_message))
                except Exception as e:
                    log.error(f"    ERROR during Twilio->Hume transcoding: {e}")

            elif event == "stop":
                log.info(f"--- Twilio 'stop' message received for CallSid: {call_sid}. Ending stream handling. ---")
                break
            
            elif event != "connected":
                 log.warning(f"--- Received unknown event type from Twilio: {event} ---")

    except WebSocketDisconnect:
        log.warning(f"--- Twilio WebSocket disconnected unexpectedly for CallSid: {call_sid} ---")
    except Exception as e:
        log.error(f"--- UNEXPECTED ERROR in handle_twilio_audio_stream for {call_sid}: {type(e).__name__} - {e} ---")
    finally:
        await cleanup_connection(call_sid, "Twilio stream ended/disconnected")


# --- Background Task to Listen to Hume ---
@app.websocket("/listen_to_hume/{call_sid}") # This decorator is harmless
async def listen_to_hume(call_sid: str):
    # ... (This function is corrected for typos) ...
    log.info(f"--- Started listening to Hume EVI for CallSid: {call_sid} ---")
    hume_ws = None
    resample_state = None
    transcript = []

    try:
        connection_details = active_connections.get(call_sid)
        if not connection_details or not connection_details.get("hume_ws"):
            log.error(f"--- listen_to_hume: Hume WS not found for {call_sid} at start. Task exiting. ---")
            return
        hume_ws = connection_details["hume_ws"]
        transcript = connection_details.get("transcript", [])


        async for message_str in hume_ws:
            connection_details = active_connections.get(call_sid)
            if not connection_details:
                 log.warning(f"--- listen_to_hume: Connection for {call_sid} disappeared. Exiting task. ---")
                 break
            
            try:
                hume_data = json.loads(message_str)
                hume_type = hume_data.get("type")

                if hume_type != "audio_output":
                    log.info(f"--- Hume Event: {hume_type} ---")

                if hume_type == "audio_output":
                    twilio_ws = connection_details.get("twilio_ws")
                    stream_sid = connection_details.get("stream_sid")

                    if twilio_ws and stream_sid and not twilio_ws.client_state == websockets.protocol.State.CLOSED:
                        try:
                            wav_b64 = hume_data["data"]
                            wav_bytes = base64.b64decode(wav_b64)
                            pcm_bytes_hume = b''
                            input_rate_hume = 8000
                            samp_width_hume = 2
                            with io.BytesIO(wav_bytes) as wav_file_like:
                                with wave.open(wav_file_like, 'rb') as wav_reader:
                                    n_channels = wav_reader.getnchannels()
                                    samp_width_hume = wav_reader.getsampwidth()
                                    input_rate_hume = wav_reader.getframerate()
                                    if n_channels != 1 or samp_width_hume != 2:
                                        log.warning(f"Unexpected WAV format from Hume: C={n_channels}, W={samp_width_hume}, R={input_rate_hume}")
                                    if samp_width_hume != 2: continue
                                    pcm_bytes_hume = wav_reader.readframes(wav_reader.getnframes())
                            if not pcm_bytes_hume: continue
                            output_rate_twilio = 8000
                            pcm_bytes_8k = pcm_bytes_hume
                            if input_rate_hume != output_rate_twilio:
                                resample_state = connection_details.get("resample_state")
                                pcm_bytes_8k, resample_state = audioop.ratecv(pcm_bytes_hume, samp_width_hume, 1, input_rate_hume, output_rate_twilio, resample_state)
                                connection_details["resample_state"] = resample_state
                            
                            # --- Typo fix: samp_width_hume ---
                            mulaw_bytes = audioop.lin2ulaw(pcm_bytes_8k, samp_width_hume)

                            mulaw_b64 = base64.b64encode(mulaw_bytes).decode('utf-8')
                            twilio_media_message = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": { "payload": mulaw_b64 }
                            }
                            await twilio_ws.send_text(json.dumps(twilio_media_message))
                        except Exception as e:
                             log.error(f"    UNEXPECTED ERROR in Hume audio processing for {call_sid}: {type(e).__name__} - {e}")
                    else:
                        log.warning(f"--- Hume sent audio, but Twilio WS/stream_sid not ready for {call_sid}. Skipping. ---")

                elif hume_type in ("user_message", "assistant_message"):
                    role = hume_data.get("message", {}).get("role", "unknown")
                    content = hume_data.get("message", {}).get("content", "")
                    
                    # --- Typo fix: {content[:30]} ---
                    transcript.append(f"{role.upper()}: {content}")
                    log.info(f"    Transcript part added: {role.upper()}: {content[:30]}...")

                elif hume_type == "tool_call":
                    tool_name = hume_data.get("tool_call", {}).get("name")
                    if tool_name == "end_call_triage":
                        log.info("--- Hume requested 'end_call_triage' tool ---")
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
                            
                            tool_response_message = {
                                "type": "tool_response",
                                "tool_call_id": hume_data.get("tool_call", {}).get("tool_call_id"),
                                "content": "{\"status\": \"success\", \"message\": \"Call triage data logged.\"}"
                            }
                            await hume_ws.send(json.dumps(tool_response_message))
                            log.info("--- Sent tool_response back to Hume ---")

                            # --- ADD THIS LOG ---
                            log.info("--- Waiting for Hume's final response... ---")
                            # ------------------
                            
                        except Exception as e:
                            log.error(f"    ERROR processing tool call: {e}")

                elif hume_type == "error":
                    log.error(f"--- Hume EVI Error (Full Message): {hume_data} ---")
                    if hume_data.get('code', '').startswith('E'):
                         log.warning(f"--- Closing connection {call_sid} due to Hume fatal error. ---")
                         break

            except json.JSONDecodeError:
                log.warning(f"--- Could not decode JSON from Hume: {message_str[:100]}... ---")
            except Exception as e:
                log.error(f"--- UNEXPECTED ERROR processing Hume message for {call_sid}: {type(e).__name__} - {e} ---")

    except websockets.exceptions.ConnectionClosed:
        log.info(f"--- Hume WebSocket closed for {call_sid}. ---")
    except Exception as e:
        log.error(f"--- UNEXPECTED ERROR in listen_to_hume main loop for {call_sid}: {type(e).__name__} - {e} ---")
    finally:
        log.info(f"--- Stopped listening to Hume EVI for {call_sid}. Triggering cleanup. ---")
        await cleanup_connection(call_sid, "Hume listener stopped")