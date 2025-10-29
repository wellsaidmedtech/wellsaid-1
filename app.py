import os
import requests
import asyncio
import websockets
import json
import logging # <-- NEW: Import logging
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect

# --- NEW: Set up basic logging ---
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
    },
    "67890": {
        "name": "John Smith",
        "dob": "1952-04-15",
        "phone_number": "+15559876543",
        "conditions": ["Asthma"],
        "medications": ["Albuterol Inhaler"]
    }
}

# --- Connection Manager ---
active_connections = {}

# --- Standard HTTP Endpoints ---

@app.get("/")
async def hello_world():
    return {"message": "Healthcare AI Server (FastAPI) is running!"}

@app.get("/api/test")
async def api_test():
    log.info("--- /api/test endpoint was called successfully! ---")
    return {"message": "This is a test message from the API."}

@app.get("/api/start-call/{patient_id}")
async def start_call(patient_id: str):
    patient_data = DUMMY_PATIENT_DB.get(patient_id)
    if patient_data:
        return patient_data
    else:
        raise HTTPException(status_code=404, detail="Patient not found")

@app.get("/api/test-hume")
async def test_hume_connection():
    log.info("--- Testing Hume API connection using manual request ---")
    if not HUME_API_KEY:
        raise HTTPException(status_code=500, detail="HUME_API_KEY missing")
    hume_configs_url = "https://api.hume.ai/v0/evi/configs"
    headers = {"X-Hume-Api-Key": HUME_API_KEY, "Accept": "application/json; charset=utf-8"}
    try:
        response = requests.get(hume_configs_url, headers=headers)
        response.raise_for_status()
        return {"status": "success", "message": "Hume API Key Strategy OK."}
    except Exception as e:
        log.error(f"--- Hume connection FAILED: {e} ---")
        raise HTTPException(status_code=500, detail=f"Failed to connect to Hume: {e}")

# --- Twilio Incoming Call Webhook ---

@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request):
    """Handles incoming calls and connects Twilio audio stream to Hume EVI."""
    log.info("-" * 30)
    log.info(">>> Twilio Incoming Call Webhook Received <<<")
    
    form_data = await request.form()
    call_sid = form_data.get('CallSid')
    from_number = form_data.get('From')
    log.info(f"  Call SID: {call_sid}")
    log.info(f"  Call From: {from_number}")

    # --- Identify Patient (Placeholder Logic) ---
    patient_id = None
    if from_number == "+19087839700": # Your number from the log
         patient_id = "12345"
    
    if not patient_id:
        log.error("--- ERROR: Could not identify patient from phone number. ---")
        response = VoiceResponse()
        response.say("Sorry, we could not identify your number.", voice='alice')
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

    patient_data = DUMMY_PATIENT_DB.get(patient_id)

    if not HUME_API_KEY or not HUME_CONFIG_ID:
        log.error("--- ERROR: Missing API Key or Config ID. ---")
        response = VoiceResponse()
        response.say("Sorry, there was a configuration error. Please try again later.", voice='alice')
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

    # --- Construct System Prompt ---
    system_prompt = f"""
    You are an empathetic AI medical assistant from WellSaid Clinic.
    Your name is 'Sam'.
    Your primary goal is to call patients for post-discharge check-ins.
    You must be kind, patient, and clear.
    You are calling patient {patient_data['name']}.
    
    Start the conversation by introducing yourself and asking if this is a good time to talk for a couple of minutes.
    
    DO NOT provide medical advice.
    Your goal is to understand their current health status and identify if they need further assistance from clinical staff.
    """
    log.info("--- Generated System Prompt ---")

    # --- Connect to Hume EVI via WebSocket ---
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

        initial_message = {
            "type": "session_settings",
            "config_id": HUME_CONFIG_ID,
            "prompt": { "text": system_prompt },
            "audio": {
                "output": {
                    "sample_rate": 8000
                }
            }
        }
        await hume_websocket.send(json.dumps(initial_message))
        log.info("--- Sent initial configuration/prompt to Hume EVI. ---")

        asyncio.create_task(listen_to_hume(call_sid))

    except Exception as e:
        log.error(f"--- FAILED to connect WebSocket to Hume EVI: {e} ---")
        response = VoiceResponse()
        response.say("Sorry, could not connect to the AI service.", voice='alice')
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

    # --- Respond to Twilio to start streaming ---
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
    """Receives audio from Twilio and forwards to Hume."""
    await websocket.accept()
    log.info(f"--- Twilio WebSocket connected for CallSid: {call_sid} ---")

    if call_sid not in active_connections:
        log.error(f"--- ERROR: Hume connection not found for CallSid: {call_sid}. Closing Twilio stream. ---")
        await websocket.close(code=1011, reason="Hume connection not established")
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
                log.info("--- Twilio 'media' event: Received audio chunk. ---") # <-- NEW DEBUG LOG
                payload = data["media"]["payload"]
                
                hume_message = {
                    "type": "audio_input",
                    "data": payload
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
    """Listens for messages from Hume EVI and forwards audio back to Twilio."""
    log.info(f"--- Started listening to Hume EVI for CallSid: {call_sid} ---")
    
    try:
        hume_ws = active_connections[call_sid]["hume_ws"]
        
        async for message_str in hume_ws:
            hume_data = json.loads(message_str)
            hume_type = hume_data.get("type")

            # --- NEW: More detailed logging ---
            if hume_type != "audio_output":
                log.info(f"--- Hume Event: {hume_type} ---")
            
            if hume_type == "audio_output":
                log.info("--- Hume Event: 'audio_output'. Received audio from Hume. ---") # <-- NEW DEBUG LOG
                
                if call_sid in active_connections and active_connections[call_sid].get("twilio_ws") and active_connections[call_sid].get("stream_sid"):
                    twilio_ws = active_connections[call_sid]["twilio_ws"]
                    stream_sid = active_connections[call_sid]["stream_sid"]
                    audio_b64 = hume_data["data"]
                    
                    log.info(f"--- Forwarding Hume audio to Twilio (Stream SID: {stream_sid}) ---") # <-- NEW DEBUG LOG

                    twilio_media_message = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {
                            "payload": audio_b64
                        }
                    }
                    await twilio_ws.send_text(json.dumps(twilio_media_message))
                
                else:
                    log.warning("--- Hume sent audio, but Twilio WS not ready. Skipping. ---")
            
            elif hume_type == "user_interruption":
                log.info("--- Hume detected user interruption ---")
            
            elif hume_type == "tool_call":
                log.warning(f"--- Hume requested tool call: {hume_data} ---")
            
            elif hume_type == "error":
                log.error(f"--- Hume EVI Error: {hume_data['error']} ---")

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