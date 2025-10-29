import os
import requests
import asyncio
import websockets
import json
import logging
import audioop
import base64
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect

# --- Set up basic logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
# ---------------------------------

app = FastAPI()

# --- Configuration & Globals ---
HUME_API_KEY = os.environ.get("HUME_API_KEY")
HUME_EVI_WS_URI = "wss://api.hume.ai/v0/evi/chat"
HUME_CONFIG_ID = os.environ.get("HUME_CONFIG_ID")

# --- Fake Patient Database ---
DUMMY_PATIENT_DB = {
    "12345": {
        "name": "Jane Doe",
        "dob": "1978-11-20",
        "phone_number": "+19087839700", # Your test number
        "conditions": ["Type 2 Diabetes", "Hypertension"],
        "medications": ["Metformin 500mg", "Lisinopril 10mg"]
    }
}

# --- Connection Manager ---
active_connections = {}

# --- Standard HTTP Endpoints ---
@app.get("/")
async def hello_world():
    return {"message": "Healthcare AI Server (FastAPI) is running!"}

# ... (other HTTP routes) ...

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

    patient_id = "+19087839700" # Hardcoding your number for this test
    if from_number != patient_id:
        log.warning(f"--- WARNING: Call from unknown number {from_number} ---")
    
    patient_data = DUMMY_PATIENT_DB["12345"]

    if not HUME_API_KEY or not HUME_CONFIG_ID:
        log.error("--- ERROR: Missing API Key or Config ID. ---")
        response = VoiceResponse()
        response.say("Sorry, there was a configuration error.", voice='alice')
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

    system_prompt = f"You are Sam, an empathetic AI medical assistant..." # (Your prompt)
    log.info("--- Generated System Prompt ---")

    try:
        log.info(f"--- Attempting WebSocket connection to Hume EVI for CallSid: {call_sid} ---")
        uri_with_key = f"{HUME_EVI_WS_URI}?apiKey={HUME_API_KEY}"
        hume_websocket = await websockets.connect(uri_with_key)
        log.info("--- WebSocket connection to Hume EVI established. ---")

        active_connections[call_sid] = {
            "hume_ws": hume_websocket,
            "twilio_ws": None,
            "stream_sid": None
        }

        # --- NEW: Correctly structure the audio config for Input AND Output ---
        initial_message = {
            "type": "session_settings",
            "config_id": HUME_CONFIG_ID,
            "prompt": { "text": system_prompt },
            "audio": {
                "input": { # Describe what we are sending to Hume
                    "sample_rate": 8000,
                    "encoding": "linear16",
                    "channels": 1
                },
                "output": { # Describe what we want Hume to send us
                    "sample_rate": 8000,
                    "encoding": "linear16",
                    "channels": 1
                }
            }
        }
        # ---------------------------------------------------------------------
        await hume_websocket.send(json.dumps(initial_message))
        log.info("--- Sent initial configuration/prompt to Hume EVI ---")

        asyncio.create_task(listen_to_hume(call_sid))

    except Exception as e:
        log.error(f"--- FAILED to connect WebSocket to Hume EVI: {e} ---")
        response = VoiceResponse()
        response.say("Sorry, could not connect to the AI service.", voice='alice')
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

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
    await websocket.accept()
    log.info(f"--- Twilio WebSocket connected for CallSid: {call_sid} ---")

    if call_sid not in active_connections:
        log.error(f"--- ERROR: Hume connection not found for CallSid: {call_sid}. ---")
        await websocket.close(code=1011)
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
                log.info("--- Twilio 'media' event ---") # <-- UNCOMMENTED
                payload = data["media"]["payload"]
                
                mulaw_bytes = base64.b64decode(payload)
                pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
                pcm_b64 = base64.b64encode(pcm_bytes).decode('utf-8')
                
                hume_message = {
                    "type": "audio_input",
                    "data": pcm_b64
                }
                await hume_ws.send(json.dumps(hume_message))

            elif event == "stop":
                log.info(f"--- Twilio 'stop' message received for CallSid: {call_sid} ---")
                break

    except WebSocketDisconnect:
        log.warning(f"--- Twilio WebSocket disconnected for CallSid: {call_sid} ---")
    except Exception as e:
        log.error(f"--- Error in Twilio audio stream for {call_sid}: {e} ---")
    finally:
        log.info(f"--- Cleaning up connections for CallSid: {call_sid} (Twilio WS closed) ---")
        if call_sid in active_connections:
            hume_ws = active_connections[call_sid].get("hume_ws")
            if hume_ws:
                await hume_ws.close()
            del active_connections[call_sid]

# --- Background Task to Listen to Hume ---
async def listen_to_hume(call_sid: str):
    log.info(f"--- Started listening to Hume EVI for CallSid: {call_sid} ---")
    
    try:
        hume_ws = active_connections[call_sid]["hume_ws"]
        
        async for message_str in hume_ws:
            hume_data = json.loads(message_str)
            hume_type = hume_data.get("type")

            if hume_type != "audio_output":
                log.info(f"--- Hume Event: {hume_type} ---")
            
            if hume_type == "audio_output":
                log.info("--- Hume Event: 'audio_output' ---") # <-- UNCOMMENTED
                
                if call_sid in active_connections and active_connections[call_sid].get("twilio_ws") and active_connections[call_sid].get("stream_sid"):
                    twilio_ws = active_connections[call_sid]["twilio_ws"]
                    stream_.sid = active_connections[call_sid]["stream_sid"]
                    
                    pcm_b64 = hume_data["data"]
                    pcm_bytes = base64.b64decode(pcm_b64)
                    mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)
                    mulaw_b64 = base64.b64encode(mulaw_bytes).decode('utf-8')
                    
                    twilio_media_message = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {
                            "payload": mulaw_b64
                        }
                    }
                    await twilio_ws.send_text(json.dumps(twilio_media_message))
                
                else:
                    log.warning("--- Hume sent audio, but Twilio WS not ready. Skipping. ---")
            
            elif hume_type == "error":
                log.error(f"--- Hume EVI Error (Full Message): {hume_data} ---")

    except websockets.exceptions.ConnectionClosedOK:
        log.info(f"--- Hume WebSocket closed normally for {call_sid}. ---")
    except Exception as e:
        log.error(f"--- Error listening to Hume for {call_sid}: {e} ---")
    finally:
        log.info(f"--- Stopped listening to Hume EVI for {call_sid}. ---")
        if call_sid in active_connections:
            twilio_ws = active_connections[call_sid].get("twilio_ws")
            if twilio_ws:
                await twilio_ws.close(code=1011, reason="Hume connection closed")