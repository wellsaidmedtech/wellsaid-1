import os
import requests
import asyncio      # Built-in async library
import websockets   # For connecting to Hume EVI
import json
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response # For TwiML
# No more Flask, flask-sock, gevent, or WsgiToAsgi needed

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
        "phone_number": "+15551234567", # Your test number
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
# This will store the active WebSocket connections for both Hume and Twilio
# Key: Twilio CallSid
# Value: {"hume_ws": WebSocket, "twilio_ws": WebSocket}
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
    print(f"--- Server received request for patient_id: {patient_id} ---")
    patient_data = DUMMY_PATIENT_DB.get(patient_id)
    if patient_data:
        print(f"Found patient: {patient_data['name']}")
        # In a real outbound scenario, you'd trigger a Twilio call here
        return patient_data
    else:
        print("--- Patient ID not found in database ---")
        raise HTTPException(status_code=404, detail="Patient not found")

@app.get("/api/test-hume")
async def test_hume_connection():
    # This function remains synchronous but is run by FastAPI in a threadpool
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
    
    # Import TwiML classes locally inside the function
    from twilio.twiml.voice_response import VoiceResponse, Connect

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
    You are an empathetic AI medical assistant... [Your full prompt text here using patient_data]
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
            "twilio_ws": None # Twilio's WS hasn't connected yet
        }

        # --- Send Initial Configuration/Prompt Message to Hume ---
        initial_message = {
            "type": "session_settings", # Or appropriate type
            "config_id": HUME_CONFIG_ID,
            "prompt": { "text": system_prompt },
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
    render_app_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "YOUR_RENDER_APP_NAME.onrender.com")
    stream_url = f"wss://{render_app_name}/twilio/audiostream/{call_sid}"
    
    print(f"--- Telling Twilio to stream audio to: {stream_url} ---")
    connect.stream(url=stream_url)
    response.append(connect)
    response.pause(length=60) # Keep call alive while streaming

    print("--- Responding to Twilio with TwiML <Connect><Stream> ---")
    return Response(content=str(response), media_type="text/xml")

# --- WebSocket Endpoint for Twilio Audio Stream ---

@app.websocket("/twilio/audiostream/{call_sid}")
async def handle_twilio_audio_stream(websocket: WebSocket, call_sid: str):
    """Receives audio from Twilio and forwards to Hume."""
    await websocket.accept()
    print(f"--- Twilio WebSocket connected for CallSid: {call_sid} ---")

    # Check if the Hume connection exists
    if call_sid not in active_connections:
        print(f"--- ERROR: Hume connection not found for CallSid: {call_sid}. Closing Twilio stream. ---")
        await websocket.close(code=1011, reason="Hume connection not established")
        return

    # Add the Twilio WebSocket to our connection manager
    active_connections[call_sid]["twilio_ws"] = websocket
    hume_ws = active_connections[call_sid]["hume_ws"]

    try:
        while True:
            # Receive audio data from Twilio
            message_str = await websocket.receive_text()
            
            # TODO: Parse Twilio's JSON message, extract the audio payload,
            #       and send it in the format Hume expects over the hume_ws.
            # print(f"Twilio msg: {message_str[:50]}...") # Debug
            
            # Example (assuming Hume just wants the raw base64 audio payload):
            # data = json.loads(message_str)
            # if data.get("event") == "media":
            #    hume_message = {
            #        "type": "audio_input",
            #        "data": data["media"]["payload"] # Send the base64 string
            #    }
            #    await hume_ws.send(json.dumps(hume_message))
            pass # Placeholder for audio forwarding

    except WebSocketDisconnect:
        print(f"--- Twilio WebSocket disconnected for CallSid: {call_sid} ---")
    except Exception as e:
        print(f"--- Error in Twilio audio stream for {call_sid}: {e} ---")
    finally:
        print(f"--- Cleaning up connections for CallSid: {call_sid} ---")
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
            # TODO: Parse Hume's message (JSON expected).
            # If it's an audio message, format it for Twilio Stream
            # and send it back over the twilio_ws.
            # print(f"Hume msg: {message_str[:50]}...") # Debug
            
            # twilio_ws = active_connections[call_sid].get("twilio_ws")
            # if twilio_ws:
            #    hume_data = json.loads(message_str)
            #    if hume_data.get("type") == "audio_output":
            #        audio_b64 = hume_data["data"]
            #        
            #        # Format for Twilio Stream (needs streamSid, which we must capture)
            #        twilio_media_message = {
            #            "event": "media",
            #            "streamSid": "...", # You get this from Twilio's 'start' message
            #            "media": { "payload": audio_b64 }
            #        }
            #        await twilio_ws.send_text(json.dumps(twilio_media_message))
            pass # Placeholder for audio forwarding

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