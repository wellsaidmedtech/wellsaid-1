import os
import requests
import asyncio # For asynchronous operations
import websockets # For connecting to Hume EVI WebSocket
import json     # For WebSocket messages
from flask import Flask, jsonify, request
from flask_sock import Sock # For handling WebSocket connections IN Flask
from twilio.twiml.voice_response import VoiceResponse, Connect # For TwiML Stream verb
from twilio.rest import Client as TwilioClient # To potentially interact with Twilio API later

app = Flask(__name__)
sock = Sock(app) # Initialize flask-sock

# --- Load Twilio Credentials (Optional but good practice) ---
# You might need these later to interact with Twilio's API directly
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
# twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) # Initialize if needed

# --- Hume Configuration ---
HUME_API_KEY = os.environ.get("HUME_API_KEY")
HUME_EVI_WS_URI = "wss://api.hume.ai/v0/evi/chat" # Hume EVI WebSocket endpoint

# --- Fake Patient Database ---
DUMMY_PATIENT_DB = {
    "12345": {
        "name": "Jane Doe",
        "dob": "1978-11-20",
        "phone_number": "+15551234567",
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

# --- Dictionary to hold active Hume connections ---
# Key: Twilio CallSid, Value: Hume WebSocket connection object
active_hume_connections = {}

# --- Standard HTTP Endpoints ---
@app.route("/")
def hello_world():
    return "Healthcare AI Server with WebSocket Support is running!"

@app.route("/api/test")
def api_test():
    print("--- /api/test endpoint was called successfully! ---")
    return jsonify(message="This is a test message from the API.")

@app.route("/api/start-call/<patient_id>")
def start_call(patient_id):
     # NOTE: This endpoint might change. Instead of initiating the Hume call here,
     # we might use Twilio's API to initiate an OUTBOUND call which then triggers
     # the /twilio/incoming_call webhook. For now, keep it as is.
    print(f"--- Server received request for patient_id: {patient_id} ---")
    patient_data = DUMMY_PATIENT_DB.get(patient_id)
    if patient_data:
        print(f"Found patient: {patient_data['name']}")
        # In a real outbound scenario, you'd trigger a Twilio call here
        return jsonify(patient_data)
    else:
        print("--- Patient ID not found in database ---")
        return jsonify(error="Patient not found"), 404

@app.route("/api/test-hume")
def test_hume_connection():
    # Keep the working manual request version for testing Hume API key
    print("--- Testing Hume API connection using manual request ---")
    api_key = os.environ.get("HUME_API_KEY")
    if not api_key: return jsonify(status="error", message="HUME_API_KEY missing"), 500

    hume_configs_url = "https://api.hume.ai/v0/evi/configs"
    headers = {"X-Hume-Api-Key": api_key, "Accept": "application/json; charset=utf-8"}
    params = {"page_size": 5}
    try:
        response = requests.get(hume_configs_url, headers=headers, params=params)
        response.raise_for_status()
        configs_data = response.json()
        config_names = [cfg.get('name', 'Unnamed Config') for cfg in configs_data.get('configs_page', [])]
        print(f"--- Hume connection SUCCESS. Found configs: {config_names} ---")
        return jsonify(status="success", message="Hume API Key Strategy OK.", available_configs=config_names)
    except Exception as e:
        print(f"--- Hume connection FAILED: {e} ---")
        return jsonify(status="error", message="Failed to connect via API Key Strategy.", error_details=str(e)), 500

# --- Twilio Incoming Call Webhook (Now Asynchronous) ---
@app.route("/twilio/incoming_call", methods=['GET', 'POST'])
async def handle_incoming_call():
    """Handles incoming calls and connects Twilio audio stream to Hume EVI."""
    print("-" * 30)
    print(">>> Twilio Incoming Call Webhook Received <<<")

    call_sid = request.values.get('CallSid', None)
    from_number = request.values.get('From', None)
    print(f"  Call SID: {call_sid}")
    print(f"  Call From: {from_number}")

    # --- Identify Patient (Placeholder Logic) ---
    # In a real app, you'd look up the from_number in your patient database
    # For now, let's assume calls from Jane Doe's number match patient "12345"
    patient_id = None
    if from_number == "+19087839700": # Your number from the log, map it to a test patient
         patient_id = "12345"
    elif from_number == DUMMY_PATIENT_DB["67890"]["phone_number"]:
         patient_id = "67890"

    if not patient_id:
        print("--- ERROR: Could not identify patient from phone number. ---")
        response = VoiceResponse()
        response.say("Sorry, we could not identify your number.", voice='alice')
        response.hangup()
        return str(response), 200, {'Content-Type': 'text/xml'}

    patient_data = DUMMY_PATIENT_DB.get(patient_id)
    hume_config_id = os.environ.get("HUME_CONFIG_ID", "YOUR_DEFAULT_CONFIG_ID_HERE") # Need a valid Config ID

    if not HUME_API_KEY or not hume_config_id or not patient_data:
        print("--- ERROR: Missing API Key, Config ID, or Patient Data. Cannot connect to Hume. ---")
        response = VoiceResponse()
        response.say("Sorry, there was a configuration error. Please try again later.", voice='alice')
        response.hangup()
        return str(response), 200, {'Content-Type': 'text/xml'}

    # --- Construct System Prompt ---
    system_prompt = f"""
    You are an empathetic AI medical assistant... [Your full prompt text here using patient_data]
    """
    print("--- Generated System Prompt ---")

    # --- Connect to Hume EVI via WebSocket ---
    try:
        print(f"--- Attempting WebSocket connection to Hume EVI for CallSid: {call_sid} ---")
        # Construct the WebSocket URI with API Key for authentication
        uri_with_key = f"{HUME_EVI_WS_URI}?apiKey={HUME_API_KEY}"
        hume_websocket = await websockets.connect(uri_with_key)
        active_hume_connections[call_sid] = hume_websocket # Store connection
        print("--- WebSocket connection to Hume EVI established. ---")

        # --- Send Initial Configuration/Prompt Message to Hume ---
        # The exact format needs to be confirmed from Hume's WebSocket API docs
        initial_message = {
            "type": "session_settings", # Or appropriate type for initial config
            "config_id": hume_config_id,
             "prompt": { "text": system_prompt },
            # Any other initial parameters Hume requires
        }
        await hume_websocket.send(json.dumps(initial_message))
        print("--- Sent initial configuration/prompt to Hume EVI. ---")

        # Start a background task to listen for messages FROM Hume
        # We pass the Twilio CallSid so the handler knows which call this is for
        asyncio.create_task(listen_to_hume(hume_websocket, call_sid))

    except Exception as e:
        print(f"--- FAILED to connect WebSocket to Hume EVI: {e} ---")
        response = VoiceResponse()
        response.say("Sorry, could not connect to the AI service.", voice='alice')
        response.hangup()
        return str(response), 200, {'Content-Type': 'text/xml'}

    # --- Respond to Twilio to start streaming ---
    response = VoiceResponse()
    connect = Connect()
    # This tells Twilio to connect via WebSocket back to THIS server
    # at the /twilio/audiostream endpoint for this specific call.
    # The URL needs to be the PUBLIC WSS (secure websocket) URL of your Render app.
    render_app_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "YOUR_RENDER_APP_NAME.onrender.com") # Render provides this env var
    stream_url = f"wss://{render_app_name}/twilio/audiostream/{call_sid}" # Include CallSid in URL
    print(f"--- Telling Twilio to stream audio to: {stream_url} ---")
    connect.stream(url=stream_url)
    response.append(connect)
    # Add a Say verb here if needed, or just let the stream handle everything.
    # response.say("Connecting to AI...", voice='alice') # Optional message while connecting
    # Twilio requires *something* after Connect, even if it's just a pause,
    # otherwise the call might hang up immediately if the stream fails fast.
    response.pause(length=60) # Keep call alive for 60 seconds while streaming

    print("--- Responding to Twilio with TwiML <Connect><Stream> ---")
    print(str(response))
    print("-" * 30)
    return str(response), 200, {'Content-Type': 'text/xml'}

# --- WebSocket Endpoint for Twilio Audio Stream ---
# flask-sock handles the WebSocket upgrade automatically
@sock.route('/twilio/audiostream/<call_sid>')
async def handle_twilio_audio_stream(ws, call_sid):
    """Receives audio from Twilio and forwards to Hume, receives audio from Hume and forwards back."""
    print(f"--- Twilio WebSocket connected for CallSid: {call_sid} ---")
    hume_websocket = active_hume_connections.get(call_sid)

    if not hume_websocket:
        print(f"--- ERROR: Hume WebSocket not found for CallSid: {call_sid}. Closing Twilio stream. ---")
        await ws.close()
        return

    # --- Bi-directional Audio Relay Loop ---
    # This needs careful implementation using asyncio to handle both directions concurrently
    async def forward_twilio_to_hume():
        try:
            while True:
                message = await ws.receive() # Receive message from Twilio
                # Twilio sends audio in specific JSON format (e.g., base64 encoded payload)
                # Parse it and forward the raw audio data to Hume WS
                # print(f"Received from Twilio: {message[:50]}...") # Debug: Print snippet
                if message:
                     # TODO: Parse Twilio message, extract audio payload (often base64),
                     #       and send it in the format Hume expects over hume_websocket.
                     # Example (Needs actual parsing logic):
                     # try:
                     #    twilio_data = json.loads(message)
                     #    if twilio_data.get("event") == "media":
                     #        audio_payload = twilio_data["media"]["payload"] # base64 string
                     #        # Send raw bytes or base64? Check Hume docs. Assume bytes for now.
                     #        await hume_websocket.send(base64.b64decode(audio_payload))
                     # except json.JSONDecodeError:
                     #    print("Non-JSON message from Twilio?") # Handle other event types
                     # except KeyError:
                     #    print("Unexpected Twilio message format")
                     pass # Placeholder
        except websockets.exceptions.ConnectionClosedOK:
            print(f"Twilio WebSocket closed normally for {call_sid}.")
        except Exception as e:
            print(f"Error receiving from Twilio or sending to Hume ({call_sid}): {e}")
        finally:
             # Ensure Hume connection is closed if Twilio disconnects
             if call_sid in active_hume_connections:
                  await active_hume_connections[call_sid].close()
                  del active_hume_connections[call_sid]
                  print(f"Cleaned up Hume connection for {call_sid} due to Twilio WS closure.")

    # Run the forwarder task
    forward_task = asyncio.create_task(forward_twilio_to_hume())

    try:
        await forward_task # Keep the connection open while forwarding
    except asyncio.CancelledError:
        print(f"Twilio audio stream handler cancelled for {call_sid}.")
    finally:
         if not forward_task.done():
              forward_task.cancel()
         print(f"Twilio WebSocket handler finished for {call_sid}.")


async def listen_to_hume(hume_ws, call_sid):
    """Listens for messages from Hume EVI and forwards audio back to Twilio."""
    # This function runs in the background for each call
    print(f"--- Started listening to Hume EVI for CallSid: {call_sid} ---")
    try:
        async for message_str in hume_ws:
            # print(f"Received from Hume: {message_str[:100]}...") # Debug: Print snippet
            # TODO: Parse Hume message (JSON expected)
            # If it's an audio message (e.g., base64 encoded), format it
            # correctly for Twilio Stream (Mu-Law payload wrapped in JSON)
            # and send it back over the Twilio WebSocket (ws object).
            # If it's a control message (e.g., end of call), handle it.
            # Example (Needs actual parsing and formatting):
            # try:
            #   hume_data = json.loads(message_str)
            #   if hume_data.get("type") == "audio_output":
            #       audio_b64 = hume_data["data"]
            #       # Convert base64 audio from Hume to format Twilio Stream expects
            #       # Twilio needs Mu-Law audio, base64 encoded, in a specific JSON structure
            #       # This conversion might require audio libraries (e.g., pydub) if formats differ
            #       twilio_media_message = {
            #           "event": "media",
            #           "streamSid": "MZ...", # Get this from Twilio's initial stream 'start' message
            #           "media": {
            #               "payload": audio_b64 # Assuming Hume provides compatible base64 audio
            #           }
            #       }
            #       # Need the Twilio WS object - how to access it here? Needs refactoring.
            #       # await twilio_ws.send(json.dumps(twilio_media_message))
            #   elif hume_data.get("type") == "user_interruption":
            #       print("Hume detected user interruption")
            #       # Send <Stop><Stream> to Twilio? Or just stop sending audio?
            #   elif hume_data.get("type") == "tool_call":
            #       print("Hume requested tool call:", hume_data)
            #       # Execute tool, send response back to Hume WS
            #   # Handle other Hume message types (errors, metadata, etc.)
            #
            # except json.JSONDecodeError:
            #   print("Non-JSON message from Hume?")
            # except KeyError:
            #   print("Unexpected Hume message format")
            pass # Placeholder
    except websockets.exceptions.ConnectionClosedOK:
        print(f"Hume WebSocket closed normally for {call_sid}.")
    except Exception as e:
        print(f"Error listening to Hume ({call_sid}): {e}")
    finally:
        print(f"Stopped listening to Hume EVI for {call_sid}.")
        # Clean up connection if it wasn't already
        if call_sid in active_hume_connections:
            # Maybe don't delete immediately, let the Twilio side handle cleanup?
            # Consider signaling the Twilio handler to close.
            pass


# --- Run Instructions ---
# Local Development: Run using uvicorn:
# uvicorn app:app --host 0.0.0.0 --port 5000 --reload
#
# Render Deployment: Update start command (see below)

if __name__ == "__main__":
    # This is less ideal for async/websockets, use uvicorn command instead
    print("Starting Flask app. For async/WebSockets, run with: uvicorn app:app --reload")
    app.run(debug=True, host='0.0.0.0', port=5000) # Port might conflict if uvicorn uses 5000