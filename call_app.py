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

# --- Basic Logging Setup ---
# Added function name and line number to logs for better debugging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s")
log = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI()

# --- Configuration Loading ---
HUME_API_KEY = os.environ.get("HUME_API_KEY")
HUME_EVI_WS_URL = "wss://api.hume.ai/v0/evi/chat"
HUME_CONFIG_ID = os.environ.get("HUME_CONFIG_ID")
RENDER_APP_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")

# Check for essential configuration at startup
missing_vars = []
if not HUME_API_KEY: missing_vars.append("HUME_API_KEY")
if not HUME_EVI_WS_URL: missing_vars.append("HUME_EVI_WS_URL")
if not HUME_CONFIG_ID: missing_vars.append("HUME_CONFIG_ID")
if not RENDER_APP_HOSTNAME: missing_vars.append("RENDER_APP_HOSTNAME")
if not TWILIO_ACCOUNT_SID: missing_vars.append("TWILIO_ACCOUNT_SID")
if not TWILIO_AUTH_TOKEN: missing_vars.append("TWILIO_AUTH_TOKEN")
if not TWILIO_PHONE_NUMBER: missing_vars.append("TWILIO_PHONE_NUMBER")

if missing_vars:
    log.critical(f"FATAL: Missing required environment variables: {', '.join(missing_vars)}. Application might not work correctly.")

# Instantiate the Twilio client
twilio_client = None
if all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN]):
    try:
        twilio_client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        log.info("Twilio REST Client initialized successfully.")
    except Exception as e:
        log.error(f"Failed to initialize Twilio REST Client: {e}", exc_info=True)
else:
    log.warning("Twilio credentials missing, outbound calls will not function.")

# --- Patient Database with MRN as Key ---
patient_mrn = "89728342" # Hard-coded MRN for testing

DUMMY_PATIENT_DB = {
    patient_mrn: {
        "phone_number": "+19087839700",
        "name": "Lon Kai",
        "date_of_birth": "03/21/1980",
        "age": 45,
        "gender": "Male",
        "medical_conditions": ["Hypertension", "Diabetes", "Hyperlipidemia"],
        "current_medications": ["Lisinopril 10 mg", "Metformin 500 mg", "Atorvastatin 20 mg"],
        "most_recent_visit": "2 weeks ago",
        "purpose_of_call": "post-visit follow up"
    }
    # Add more dummy patients here if needed
}
log.info(f"Loaded dummy patient 'Lon Kai' with MRN: {patient_mrn}")

# --- Patient Lookup Functions ---
def get_patient_info_by_phone(phone_number: str) -> dict | None:
    if not phone_number: return None
    for mrn, data in DUMMY_PATIENT_DB.items():
        if data.get("phone_number") == phone_number:
            return {"mrn": mrn, **data}
    return None

def get_patient_info_by_mrn(mrn: str) -> dict | None:
    if not mrn: return None
    data = DUMMY_PATIENT_DB.get(mrn)
    if data:
        return {"mrn": mrn, **data}
    return None

# --- WebSocket Connection Management ---
active_connections = {} # Key: CallSid, Value: Dict

# --- Helper Function for Cleanup ---
async def cleanup_connection(call_sid: str, reason: str = "Unknown"):
    """Safely closes WebSockets and removes the connection entry."""
    if not call_sid:
        log.warning("cleanup_connection called with no CallSid.")
        return

    log.info(f"Cleaning up connections for CallSid: {call_sid} (Reason: {reason})")
    connection_details = active_connections.pop(call_sid, None)

    if connection_details:
        hume_ws = connection_details.get("hume_ws")
        twilio_ws = connection_details.get("twilio_ws")

        if hume_ws and hume_ws.state != websockets.protocol.State.CLOSED:
            try:
                log.info(f"Closing Hume WS for {call_sid}")
                await hume_ws.close(code=1000, reason=f"Cleanup: {reason}")
            except Exception as e:
                log.error(f"Error closing Hume WS for {call_sid}: {e}", exc_info=True)
        
        if twilio_ws and twilio_ws.client_state != websockets.protocol.State.CLOSED:
             try:
                 log.info(f"Closing Twilio WS for {call_sid}")
                 await twilio_ws.close(code=1000, reason=f"Cleanup: {reason}")
             except Exception as e:
                 log.error(f"Error closing Twilio WS for {call_sid}: {e}", exc_info=True)
    else:
        log.warning(f"Cleanup called for {call_sid}, but no active connection found (might have been cleaned up already).")

# --- Core API Endpoints ---
@app.get("/")
async def root():
    """Basic health check endpoint."""
    return {"message": "Healthcare AI Server (FastAPI) is running!"}

# --- Endpoint to Initiate Outbound Call ---
class StartCallRequest(BaseModel):
    mrn: str

@app.post("/api/start_call")
async def start_outbound_call(call_request: StartCallRequest):
    """
    Triggers an outbound call to a patient using their MRN.
    Passes the patient's MRN in the webhook URL.
    """
    mrn = call_request.mrn
    log.info(f"Received request to call patient with MRN: {mrn}")

    if not twilio_client:
        log.error("Cannot place call: Twilio client is not initialized.")
        raise HTTPException(status_code=503, detail="Twilio client not available. Check server configuration.")

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
    """
    Webhook hit by Twilio when the outbound call is answered.
    Gets patient MRN from URL and connects to Hume EVI.
    """
    log.info("-" * 30)
    log.info("Twilio Call Webhook Received (Outbound-Answered)")
    call_sid = None # Initialize call_sid for potential use in finally block

    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid')
        from_number = form_data.get('From') # Patient's number
        mrn = request.query_params.get('mrn')

        if not call_sid or not mrn:
             log.error(f"Missing CallSid or MRN in request. CallSid: {call_sid}, MRN: {mrn}")
             response = VoiceResponse(); response.say("System configuration error."); response.hangup()
             return Response(content=str(response), media_type="text/xml", status_code=200)

        log.info(f"CallSid: {call_sid}, Patient Number: {from_number}, MRN from URL: {mrn}")

        # --- 1. Identify Patient (using MRN) ---
        patient_data = get_patient_info_by_mrn(mrn)
        if not patient_data:
            log.error(f"Could not find patient for MRN {mrn} provided in webhook URL. CallSid: {call_sid}")
            response = VoiceResponse(); response.say("System error: Could not retrieve patient records.", voice='alice'); response.hangup()
            return Response(content=str(response), media_type="text/xml", status_code=200)

        log.info(f"Identified Patient: {patient_data.get('name', 'N/A')} (MRN: {mrn})")

        # --- 2. Connect to Hume EVI ---
        if not HUME_API_KEY or not HUME_EVI_WS_URL or not HUME_CONFIG_ID:
            log.error(f"Hume configuration missing. Cannot connect. CallSid: {call_sid}")
            response = VoiceResponse(); response.say("AI service configuration error."); response.hangup()
            return Response(content=str(response), media_type="text/xml", status_code=200)
            
        uri_with_key_and_config = (
            f"{HUME_EVI_WS_URL}?apiKey={HUME_API_KEY}"
            f"&config_id={HUME_CONFIG_ID}"
            f"&verbose_transcription=true"
        )
        log.info(f"Connecting to Hume EVI WebSocket... CallSid: {call_sid}")
        
        try:
             hume_websocket = await websockets.connect(uri_with_key_and_config)
        except websockets.exceptions.InvalidURI:
             log.error(f"Invalid Hume WebSocket URI: {HUME_EVI_WS_URL}. CallSid: {call_sid}", exc_info=True)
             response = VoiceResponse(); response.say("AI connection error: Invalid address."); response.hangup()
             return Response(content=str(response), media_type="text/xml", status_code=200)
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
            "is_interrupted": False
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
                "parameters": json.dumps(tool_params_schema) # Stringified
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

        # Start the background listener task *only after* settings are sent successfully
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
    """
    Receives audio (mu-law) from Twilio, transcodes, forwards to Hume.
    """
    connection_details = active_connections.get(call_sid)
    if not connection_details or not connection_details.get("hume_ws"):
        log.error(f"Twilio WS connected, but no active Hume connection found for CallSid: {call_sid}. Closing immediately.")
        return

    try:
        await websocket.accept()
        log.info(f"Twilio WebSocket accepted for CallSid: {call_sid}")
        connection_details["twilio_ws"] = websocket # Store WS only after accept
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


# --- Background Task to Listen to Hume ---
@app.websocket("/listen_to_hume/{call_sid}") # This decorator is harmless
async def listen_to_hume(call_sid: str):
    """
    Listens for messages from Hume EVI. Handles interruptions, audio, transcript, tools.
    """
    log.info(f"Started listening to Hume EVI for CallSid: {call_sid}")
    hume_ws = None
    transcript = []
    is_interrupted = False

    try:
        connection_details = active_connections.get(call_sid)
        if not connection_details or not connection_details.get("hume_ws") or connection_details["hume_ws"].closed:
            log.error(f"listen_to_hume: Hume WS not found or already closed for {call_sid} at start. Task exiting.")
            return

        hume_ws = connection_details["hume_ws"]
        transcript = connection_details.get("transcript", [])
        is_interrupted = connection_details.get("is_interrupted", False)


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
                    if connection_details: # Reset interrupt flag
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
                        
                        tool_response_message = {
                            "type": "tool_response",
                            "tool_call_id": tool_call_id,
                            "content": json.dumps({"status": "success", "message": "Call triage data logged."}) # Content must be JSON string
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