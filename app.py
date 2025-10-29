import os
import requests
import asyncio
import websockets
import json
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
# We also need to import Twilio's TwiML classes at the top for the HTTP route
from twilio.twiml.voice_response import VoiceResponse, Connect

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
# Key: Twilio CallSid
# Value: {"hume_ws": WebSocket, "twilio_ws": WebSocket, "stream_sid": str}
active_connections = {}

# --- Standard HTTP Endpoints ---

@app.get("/")
async def hello_world():
    return {"message": "Healthcare AI Server (FastAPI) is running!"}

@app.get("/api/test")
async def api_test():
    print("--- /api/test endpoint was called successfully! ---")
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
    print("--- Testing Hume API connection using manual request ---")
    if not HUME_API_KEY:
        raise HTTPException(status_code=500, detail="HUME_API_KEY missing")
    hume_configs_url = "https://api.hume.ai/v0/evi/configs"
    headers = {"X-Hume-Api-Key": HUME_API_KEY, "Accept": "application/json; charset=utf-8"}
    try:
        response = requests.get(hume_configs_url, headers=headers)
        response.raise_for_status()
        return {"status": "success", "message": "Hume API Key Strategy OK."}
    except Exception as e:
        print(f"--- Hume connection FAILED: {e} ---")
        raise HTTPException(status_code=500, detail=f"Failed to connect to Hume: {e}")

# --- Twilio Incoming Call Webhook ---

@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request):
    """Handles incoming calls and connects Twilio audio stream to Hume EVI."""
    print("-" * 30)
    print(">>> Twilio Incoming Call Webhook Received <<<")
    
    form_data = await request.form()
    call_sid = form_data.get('CallSid')
    from_number = form_data.get('From')
    print(f"  Call SID: {call_sid}")
    print(f"  Call From: {from_number}")

    # --- Identify Patient (Placeholder Logic) ---
    patient_id = None
    if from_number == "+19087839700": # Your number from the log
         patient_id = "12345"
    
    if not patient_id:
        print("--- ERROR: Could not identify patient from phone number. ---")
        response = VoiceResponse()
        response.say("Sorry, we could not identify your number.", voice='alice')
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

    patient_data = DUMMY_PATIENT_DB.get(patient_id)

    if not HUME_API_KEY or not HUME_CONFIG_ID:
        print("--- ERROR: Missing API Key or Config ID. ---")
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
    print("--- Generated System Prompt ---")

    # --- Connect to Hume EVI via WebSocket ---
    try:
        print(f"--- Attempting WebSocket connection to Hume EVI for CallSid: {call_sid} ---")
        uri_with_key = f"{HUME_EVI_WS_URI}?apiKey={HUME_API_KEY}"
        hume_websocket = await websockets.connect(uri_with_key)
        print("--- WebSocket connection to Hume EVI established. ---")

        # Store the connection
        active_connections[call_sid] = {
            "hume_ws": hume_websocket,
            "twilio_ws": None, # Twilio's WS hasn't connected yet
            "stream_sid": None # We get this from Twilio's 'start' message
        }

        # --- Send Initial Configuration/Prompt Message to Hume ---
        initial_message = {
            "type": "session_settings",
            "config_id": HUME_CONFIG_ID,
            "prompt": { "text": system_prompt },
            "audio": {
                "output": {
                    "sample_rate": 8000  # Tell Hume to output at 8kHz
                }
            }
        }
        await hume_websocket.send(json.dumps(initial_message))
        print("--- Sent initial configuration/prompt to Hume EVI. ---")

        # Start the background task to listen for messages FROM Hume
        asyncio.create_task(listen_to_hume(call_sid))

    except Exception as e:
        print(f"--- FAILED to connect WebSocket to Hume EVI: {e} ---")
        response = VoiceResponse()
        response.say("Sorry, could not connect to the AI service.", voice='alice')
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

    # --- Respond to Twilio to start streaming ---
    response = VoiceResponse()
    connect = Connect()
    render_app_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    stream_url = f"wss://{render_app_name}/twilio/audiostream/{call_sid}"
    
    print(f"--- Telling Twilio to stream audio to: {stream_url} ---")
    connect.stream(url=stream_url)
    response.append(connect)
    response.pause(length=60) 

    print("--- Responding to Twilio with TwiML <Connect><Stream> ---")
    return Response(content=str(response), media_type="text/xml")

# --- WebSocket Endpoint for Twilio Audio Stream ---

@app.websocket("/twilio/audiostream/{call_sid}")
async def handle_twilio_audio_stream(websocket: WebSocket, call_sid: str):
    """Receives audio from Twilio and forwards to Hume."""
    await websocket.accept()
    print(f"--- Twilio WebSocket connected for CallSid: {call_sid} ---")

    if call_sid not in active_connections:
        print(f"--- ERROR: Hume connection not found for CallSid: {call_sid}. Closing Twilio stream. ---")
        await websocket.close(code=1011, reason="Hume connection not established")
        return

    active_connections[call_sid]["twilio_ws"] = websocket
    hume_ws = active_connections[call_sid]["hume_ws"]

    try:
        while True:
            # Receive audio data from Twilio
            message_str = await websocket.receive_text()
            data = json.loads(message_str)

            # --- NEW: Handle different event types from Twilio ---
            if data["event"] == "start":
                stream_sid = data["start"]["streamSid"]
                active_connections[call_sid]["stream_sid"] = stream_sid
                print(f"--- Twilio 'start' message received, Stream SID: {stream_sid} ---")
            
            elif data["event"] == "media":
                # This is the audio payload
                payload = data["media"]["payload"] # This is base64 audio
                
                # --- NEW: Forward the audio to Hume EVI ---
                hume_message = {
                    "type": "audio_input",
                    "data": payload # Send the base64 string
                }
                await hume_ws.send(json.dumps(hume_message))

            elif data["event"] == "stop":
                print(f"--- Twilio 'stop' message received for CallSid: {call_sid} ---")
                break # Exit the loop, which will trigger the finally block

    except WebSocketDisconnect:
        print(f"--- Twilio WebSocket disconnected for CallSid: {call_sid} ---")
    except Exception as e:
        print(f"--- Error in Twilio audio stream for {call_sid}: {e} ---")
    finally:
        print(f"--- Cleaning up connections for CallSid: {call_sid} (Twilio WS closed) ---")
        if call_sid in active_connections:
            hume_ws = active_connections[call_sid].get("hume_ws")
            if hume_ws:
                await hume_ws.close()
            del active_connections[call_sid]

# --- Background Task to Listen to Hume ---

async def listen_to_hume(call_sid: str):
    """Listens for messages from Hume EVI and forwards audio back to Twilio."""
    print(f"--- Started listening to Hume EVI for CallSid: {call_sid} ---")
    
    try:
        hume_ws = active_connections[call_sid]["hume_ws"]
        
        async for message_str in hume_ws:
            hume_data = json.loads(message_str)
            
            # --- NEW: Check for audio from Hume ---
            if hume_data.get("type") == "audio_output":
                # We have audio from Hume, send it to Twilio
                
                # Check if Twilio's connection is ready
                if call_sid in active_connections and active_connections[call_sid].get("twilio_ws") and active_connections[call_sid].get("stream_sid"):
                    twilio_ws = active_connections[call_sid]["twilio_ws"]
                    stream_sid = active_connections[call_sid]["stream_sid"]
                    audio_b64 = hume_data["data"]

                    # --- NEW: Format the message for Twilio ---
                    twilio_media_message = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {
                            "payload": audio_b64
                        }
                    }
                    await twilio_ws.send_text(json.dumps(twilio_media_message))
                
                else:
                    print("--- Hume sent audio, but Twilio WS not ready. Skipping. ---")
            
            elif hume_data.get("type") == "user_interruption":
                print("--- Hume detected user interruption ---")
            
            elif hume_data.get("type") == "tool_call":
                print(f"--- Hume requested tool call: {hume_data} ---")
                # TODO: Handle tool calls (like MedlinePlus) here
            
            elif hume_data.get("type") == "error":
                print(f"--- Hume EVI Error: {hume_data['error']} ---")
                
            # Handle other Hume message types if needed...

    except websockets.exceptions.ConnectionClosedOK:
        print(f"--- Hume WebSocket closed normally for {call_sid}. ---")
    except Exception as e:
        print(f"--- Error listening to Hume for {call_sid}: {e} ---")
    finally:
        print(f"--- Stopped listening to Hume EVI for {call_sid}. ---")
        # Ensure cleanup if Hume disconnects first
        if call_sid in active_connections:
            twilio_ws = active_connections[call_sid].get("twilio_ws")
            if twilio_ws:
                await twilio_ws.close(code=1011, reason="Hume connection closed")
            # The twilio_ws disconnect will trigger the final cleanup