import os
import requests
import asyncio
import websockets
import json
import logging
import audioop
import base64
import wave     # <-- NEW: Import for WAV parsing
import io       # <-- NEW: For in-memory file handling
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect

# --- Logging setup ... (keep as is) ...
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

# --- Config, DB, Connection Manager ... (keep as is) ...
HUME_API_KEY = os.environ.get("HUME_API_KEY")
HUME_EVI_WS_URI = "wss://api.hume.ai/v0/evi/chat"
HUME_CONFIG_ID = os.environ.get("HUME_CONFIG_ID")
DUMMY_PATIENT_DB = {
    "12345": {
        "name": "Jane Doe",
        "dob": "1978-11-20",
        "phone_number": "+19087839700", # Your test number
        "conditions": ["Type 2 Diabetes", "Hypertension"],
        "medications": ["Metformin 500mg", "Lisinopril 10mg"]
    }
}
active_connections = {}

# --- HTTP Endpoints ... (keep as is) ...
@app.get("/")
async def hello_world(): return {"message": "Healthcare AI Server (FastAPI) is running!"}


# --- Twilio Incoming Call Webhook ---
@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request):
    log.info("-" * 30)
    log.info(">>> Twilio Incoming Call Webhook Received <<<")

    form_data = await request.form()
    call_sid = form_data.get('CallSid')
    from_number = form_data.get('From')
    log.info(f"  Call SID: {call_sid}")
    log.info(f"  Call From: {from_number}")

    patient_id = "+19087839700" # Hardcoding for test
    if from_number != patient_id:
        log.warning(f"--- WARNING: Call from unknown number {from_number} ---")

    patient_data = DUMMY_PATIENT_DB["12345"]

    if not HUME_API_KEY or not HUME_CONFIG_ID:
        log.error("--- ERROR: Missing API Key or Config ID. ---")
        # ... (error response handling) ...
        response = VoiceResponse(); response.say("Config error."); response.hangup()
        return Response(content=str(response), media_type="text/xml")

    system_prompt = f"You are Sam, an empathetic AI medical assistant..." # (Your prompt)
    log.info("--- Generated System Prompt ---")

    try:
        log.info(f"--- Attempting WebSocket connection to Hume EVI for CallSid: {call_sid} ---")
        uri_with_key = f"{HUME_EVI_WS_URI}?apiKey={HUME_API_KEY}"
        hume_websocket = await websockets.connect(uri_with_key)
        log.info("--- WebSocket connection to Hume EVI established. ---")

        active_connections[call_sid] = {
            "hume_ws": hume_websocket, "twilio_ws": None, "stream_sid": None
        }

        # --- CORRECTED: Use 'encoding' key ---
        initial_message = {
            "type": "session_settings",
            "config_id": HUME_CONFIG_ID,
            "prompt": { "text": system_prompt },
            "audio": {
                "encoding": "linear16",  # <-- Use 'encoding' as per error msg
                "sample_rate": 8000,
                "channels": 1
            }
        }
        # ------------------------------------
        await hume_websocket.send(json.dumps(initial_message))
        log.info("--- Sent initial configuration/prompt to Hume EVI ---")

        asyncio.create_task(listen_to_hume(call_sid))

    except Exception as e:
        log.error(f"--- FAILED to connect WebSocket to Hume EVI: {e} ---")
        # ... (error response handling) ...
        response = VoiceResponse(); response.say("AI connect error."); response.hangup()
        return Response(content=str(response), media_type="text/xml")

    # --- Respond to Twilio ---
    response = VoiceResponse()
    connect = Connect()
    render_app_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    stream_url = f"wss://{render_app_name}/twilio/audiostream/{call_sid}"
    log.info(f"--- Telling Twilio to stream audio to: {stream_url} ---")
    connect.stream(url=stream_url)
    response.append(connect)
    response.pause(length=60)
    log.info("--- Responding to Twilio with TwiML <Connect><Stream> ---")
    return Response(content=str(response), media_type="text/xml")

# --- WebSocket Endpoint for Twilio Audio Stream ---
@app.websocket("/twilio/audiostream/{call_sid}")
async def handle_twilio_audio_stream(websocket: WebSocket, call_sid: str):
    await websocket.accept() # Accept connection immediately
    log.info(f"--- Twilio WebSocket connected for CallSid: {call_sid} ---")

    if call_sid not in active_connections:
        log.error(f"--- ERROR: Hume connection not found for CallSid: {call_sid}. Closing Twilio stream. ---")
        await websocket.close(code=1011) # Use await here
        return

    active_connections[call_sid]["twilio_ws"] = websocket
    hume_ws = active_connections[call_sid]["hume_ws"]

    try:
        while True:
            message_str = await websocket.receive_text()
            data = json.loads(message_str)
            event = data.get("event")

            if event == "start":
                stream_sid = data["start"]["streamSid"]
                active_connections[call_sid]["stream_sid"] = stream_sid
                log.info(f"--- Twilio 'start' message received, Stream SID: {stream_sid} ---")

            elif event == "media":
                # log.info("--- Twilio 'media' event ---") # Still potentially noisy
                payload = data["media"]["payload"]

                try:
                    mulaw_bytes = base64.b64decode(payload)
                    # log.info(f"    Received {len(mulaw_bytes)} mulaw bytes from Twilio.")

                    # Convert mulaw to PCM (little-endian is expected by Hume docs)
                    pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
                    # log.info(f"    Converted to {len(pcm_bytes)} PCM bytes (Little Endian) for Hume.")

                    pcm_b64 = base64.b64encode(pcm_bytes).decode('utf-8')

                    hume_message = { "type": "audio_input", "data": pcm_b64 }
                    await hume_ws.send(json.dumps(hume_message))
                except Exception as e:
                    log.error(f"    ERROR during Twilio->Hume transcoding: {e}")

            elif event == "stop":
                log.info(f"--- Twilio 'stop' message received for CallSid: {call_sid} ---")
                break

    # Make sure to handle WebSocketDisconnect specifically first
    except WebSocketDisconnect:
        log.warning(f"--- Twilio WebSocket disconnected for CallSid: {call_sid} ---")
    except websockets.exceptions.ConnectionClosedOK: # Catch potential Hume closure cleanly
         log.info(f"--- Hume WS closed while handling Twilio stream for {call_sid} ---")
    except Exception as e:
        # Catch unexpected errors, log type and message
        log.error(f"--- Error in Twilio audio stream for {call_sid}: {type(e).__name__} - {e} ---")
    finally:
        log.info(f"--- Cleaning up connections for CallSid: {call_sid} (Twilio WS closed) ---")
        if call_sid in active_connections:
            hume_ws = active_connections[call_sid].get("hume_ws")
            if hume_ws and not hume_ws.closed: # Check if not already closed
                await hume_ws.close()
            # Remove even if Hume WS was already closed or None
            if call_sid in active_connections:
                 del active_connections[call_sid]


# --- Background Task to Listen to Hume ---
async def listen_to_hume(call_sid: str):
    log.info(f"--- Started listening to Hume EVI for CallSid: {call_sid} ---")
    hume_ws = None # Initialize
    try:
        # Ensure connection exists before proceeding
        if call_sid not in active_connections or not active_connections[call_sid].get("hume_ws"):
            log.error(f"--- listen_to_hume: Hume WS not found for {call_sid} at start. ---")
            return
        hume_ws = active_connections[call_sid]["hume_ws"]

        async for message_str in hume_ws:
            # Check again if connection still valid before processing
            if call_sid not in active_connections:
                 log.warning(f"--- listen_to_hume: Connection for {call_sid} cleaned up elsewhere. Exiting task. ---")
                 break
            
            hume_data = json.loads(message_str)
            hume_type = hume_data.get("type")

            if hume_type != "audio_output":
                log.info(f"--- Hume Event: {hume_type} ---")

            if hume_type == "audio_output":
                log.info("--- Hume Event: 'audio_output' ---")

                # Check if Twilio connection is still active and ready
                conn_details = active_connections.get(call_sid)
                if conn_details and conn_details.get("twilio_ws") and conn_details.get("stream_sid"):
                    twilio_ws = conn_details["twilio_ws"]
                    stream_sid = conn_details["stream_sid"]

                    try:
                        # --- NEW: Parse WAV and transcode ---
                        wav_b64 = hume_data["data"]
                        wav_bytes = base64.b64decode(wav_b64)

                        # Use BytesIO to treat the bytes as an in-memory file
                        with io.BytesIO(wav_bytes) as wav_file_like:
                            with wave.open(wav_file_like, 'rb') as wav_reader:
                                # Verify format (optional but good practice)
                                if wav_reader.getnchannels() != 1 or wav_reader.getsampwidth() != 2 or wav_reader.getframerate() != 8000:
                                    log.warning(f"Unexpected WAV format from Hume: C={wav_reader.getnchannels()}, W={wav_reader.getsampwidth()}, R={wav_reader.getframerate()}")
                                    # Continue anyway, audioop might handle it or fail

                                pcm_bytes = wav_reader.readframes(wav_reader.getnframes())
                                # log.info(f"    Read {len(pcm_bytes)} PCM bytes from Hume WAV.")

                        # Transcode the extracted PCM to mulaw
                        mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)
                        # log.info(f"    Converted to {len(mulaw_bytes)} mulaw bytes for Twilio.")

                        mulaw_b64 = base64.b64encode(mulaw_bytes).decode('utf-8')
                        # ------------------------------------

                        twilio_media_message = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": { "payload": mulaw_b64 }
                        }
                        # Check if Twilio WS is still open before sending
                        if not twilio_ws.client_state == websockets.protocol.State.CLOSED:
                             await twilio_ws.send_text(json.dumps(twilio_media_message))
                        else:
                             log.warning(f"--- Hume sent audio, but Twilio WS for {call_sid} was already closed. ---")
                             break # Stop listening if Twilio is gone

                    except wave.Error as e:
                         log.error(f"    ERROR parsing Hume WAV data: {e}")
                    except Exception as e:
                         log.error(f"    ERROR during Hume->Twilio transcoding or sending: {type(e).__name__} - {e}")

                else:
                    log.warning(f"--- Hume sent audio, but Twilio WS/stream_sid not ready for {call_sid}. Skipping. ---")
                    # If Twilio never connected or stream_sid wasn't received, we might end up here.

            elif hume_type == "error":
                log.error(f"--- Hume EVI Error (Full Message): {hume_data} ---")
                # Consider closing the connection if Hume sends a critical error
                # break

    except websockets.exceptions.ConnectionClosedOK:
        log.info(f"--- Hume WebSocket closed normally for {call_sid}. ---")
    except websockets.exceptions.ConnectionClosedError as e:
        log.warning(f"--- Hume WebSocket closed with error for {call_sid}: {e} ---")
    except Exception as e:
        log.error(f"--- Error listening to Hume for {call_sid}: {type(e).__name__} - {e} ---")
    finally:
        log.info(f"--- Stopped listening to Hume EVI for {call_sid}. ---")
        # Clean up if Hume disconnects first
        if call_sid in active_connections:
            twilio_ws = active_connections[call_sid].get("twilio_ws")
            # Close Twilio WS only if it exists and is still open
            if twilio_ws and not twilio_ws.client_state == websockets.protocol.State.CLOSED:
                await twilio_ws.close(code=1011, reason="Hume connection closed")
            # Remove entry regardless of Twilio state, Hume is gone
            if call_sid in active_connections:
                del active_connections[call_sid]