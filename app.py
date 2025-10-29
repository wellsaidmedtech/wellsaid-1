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
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Connect

# --- Basic Logging Setup ---
# Logs will include timestamp, level, and message
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI()

# --- Configuration Loading ---
# Load credentials and settings from environment variables
HUME_API_KEY = os.environ.get("HUME_API_KEY")
HUME_EVI_WS_URI = "wss://api.hume.ai/v0/evi/chat"
HUME_CONFIG_ID = os.environ.get("HUME_CONFIG_ID") # Specific EVI config (voice, model, etc.)
RENDER_APP_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME") # Render's public hostname

# Simple check for essential configuration
if not all([HUME_API_KEY, HUME_CONFIG_ID, RENDER_APP_HOSTNAME]):
    log.error("FATAL: Missing one or more required environment variables (HUME_API_KEY, HUME_CONFIG_ID, RENDER_EXTERNAL_HOSTNAME).")
    # In a real app, you might exit or raise a more specific config error
    # For now, we log the error, and the app might fail later at runtime.

# --- Simple Patient Lookup ---
# In a real application, this would query a secure database.
DUMMY_PATIENT_DB = {
    "+19087839700": { # Using phone number as key for simplicity
        "id": "12345",
        "name": "Jane Doe",
        "dob": "1978-11-20",
        "conditions": ["Type 2 Diabetes", "Hypertension"],
        "medications": ["Metformin 500mg", "Lisinopril 10mg"]
    }
    # Add other test patients here if needed
}

def get_patient_info(phone_number: str) -> dict | None:
    """Looks up patient data based on phone number."""
    return DUMMY_PATIENT_DB.get(phone_number)

# --- WebSocket Connection Management ---
# Stores active Hume and Twilio WebSocket connections, keyed by Twilio CallSid.
# Value: {"hume_ws": WebSocket, "twilio_ws": WebSocket, "stream_sid": str, "resample_state": tuple | None}
active_connections = {}

# --- Helper Function for Cleanup ---
async def cleanup_connection(call_sid: str, reason: str = "Unknown"):
    """Safely closes WebSockets and removes the connection entry."""
    log.info(f"--- Cleaning up connections for CallSid: {call_sid} (Reason: {reason}) ---")
    connection_details = active_connections.pop(call_sid, None) # Remove and get details

    if connection_details:
        hume_ws = connection_details.get("hume_ws")
        twilio_ws = connection_details.get("twilio_ws")

        if hume_ws and not hume_ws.closed:
            log.info(f"    Closing Hume WS for {call_sid}")
            await hume_ws.close(code=1000, reason=f"Cleanup: {reason}")
        if twilio_ws and not twilio_ws.client_state == websockets.protocol.State.CLOSED:
             log.info(f"    Closing Twilio WS for {call_sid}")
             await twilio_ws.close(code=1000, reason=f"Cleanup: {reason}")
    else:
        log.warning(f"--- Cleanup called for {call_sid}, but no active connection found. ---")


# --- Core API Endpoints ---

@app.get("/")
async def root():
    """Basic health check endpoint."""
    return {"message": "Healthcare AI Server (FastAPI) is running!"}

# --- Twilio Incoming Call Webhook ---
@app.post("/twilio/incoming_call")
async def handle_incoming_call(request: Request):
    """
    Handles incoming Twilio calls. WORKAROUND: Creates a temporary config via REST API first.
    1. Identifies the patient.
    2. Constructs a system prompt.
    3. Creates a temporary Hume config via REST API with the prompt and voice.
    4. Connects to Hume EVI via WebSocket using the temporary config ID.
    5. Sends initial audio settings to Hume.
    6. Responds to Twilio with TwiML to start audio streaming.
    """
    log.info("-" * 30)
    log.info(">>> Twilio Incoming Call Webhook Received <<<")
    temp_config_id = None # Initialize in case of early errors

    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid')
        from_number = form_data.get('From')

        if not call_sid or not from_number:
             log.error("--- ERROR: Missing CallSid or From number in Twilio request. ---")
             raise HTTPException(status_code=400, detail="Missing required call information")

        log.info(f"  Call SID: {call_sid}")
        log.info(f"  Call From: {from_number}")

        # --- 1. Identify Patient ---
        patient_data = get_patient_info(from_number)
        if not patient_data:
            log.warning(f"--- WARNING: Could not identify patient from {from_number}. Rejecting call. ---")
            response = VoiceResponse()
            response.say("Sorry, we could not identify your phone number with our records.", voice='alice')
            response.hangup()
            return Response(content=str(response), media_type="text/xml")

        log.info(f"--- Identified Patient: {patient_data.get('name', 'N/A')} (ID: {patient_data.get('id', 'N/A')}) ---")

        # --- 2. Construct System Prompt ---
        conditions_list = ", ".join(patient_data.get('conditions', ['N/A']))
        medications_list = ", ".join(patient_data.get('medications', ['N/A']))
        system_prompt = f"""
        **Persona:**
        You are 'Sam', an empathetic AI medical assistant from WellSaid Clinic performing a routine post-discharge check-in call.
        Your tone should be warm, caring, patient, and professional. Use clear, simple language.
        Your primary goal is to assess the patient's current well-being, adherence to discharge instructions, and identify any urgent needs requiring clinical follow-up.
        **DO NOT provide medical advice, diagnoses, or treatment recommendations.** If asked for advice, politely redirect the patient to contact their doctor or the clinic.

        **Patient Context:**
        * **Name:** {patient_data.get('name', 'the patient')}
        * **Relevant Conditions:** {conditions_list}
        * **Current Medications:** {medications_list}

        **Conversation Flow:**
        1.  **Introduction:** Introduce yourself ("Hi, this is Sam calling from WellSaid Clinic...") and verify you're speaking with {patient_data.get('name', 'the patient')}. Ask if this is a good time to talk for a few minutes about their recovery.
        2.  **General Well-being:** Ask how they are feeling overall since being discharged.
        3.  **Specific Inquiry (Use Context):** Ask about how they are managing their specific conditions (e.g., "How have your blood sugar levels been since you started the Metformin again?" or "Have you experienced any side effects from the Lisinopril?"). **Acknowledge their known conditions and medications when relevant.**
        4.  **Adherence Check:** Ask if they've been able to take their medications ({medications_list}) as prescribed and follow any specific discharge instructions.
        5.  **Identify Concerns:** Ask if they have any specific concerns, new symptoms, or questions they'd like to pass on to their doctor.
        6.  **Closing:** Summarize briefly (e.g., "Okay, it sounds like things are generally going well, but I'll make a note about [specific concern] for your doctor."). Explain the next steps (e.g., "Your doctor will review this, and someone from the clinic will reach out if needed."). Thank them for their time.

        **Important:**
        * Listen actively and respond empathetically.
        * If the patient mentions a severe symptom (e.g., chest pain, difficulty breathing, severe confusion), instruct them to call 911 or go to the nearest emergency room immediately, and state you will also alert the clinic.
        * Keep the call concise, aiming for 2-5 minutes.
        """
        log.info("--- Generated Enhanced System Prompt ---")

        # --- 3. Create Temporary Hume Config via REST API ---
        log.info(f"--- Creating temporary Hume config for CallSid: {call_sid} ---")
        rest_api_url = "https://api.hume.ai/v0/evi/configs"
        headers = {
            "X-Hume-Api-Key": HUME_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "name": f"Call_{call_sid}", # Give it a unique temporary name
            "evi_version": "3",
            "prompt": {
                "text": system_prompt # Inject dynamic prompt here
            },
            "voice": {
                "id": "97fe9008-8584-4d56-8453-bd8c7ead3663", # Your desired voice
                "provider": "HUME_AI"
            },
            # Add other base settings if needed, e.g., language model
            # "language_model": {
            #    "model_provider": "ANTHROPIC",
            #    "model_resource": "claude-3-haiku-20240307", # Example
            #    "temperature": 0.7
            # }
        }
        response = requests.post(rest_api_url, headers=headers, json=payload)
        response.raise_for_status() # Check for HTTP errors (4xx, 5xx)

        config_data = response.json()
        temp_config_id = config_data.get("id")

        if not temp_config_id:
             log.error("--- FAILED to create Hume config: ID missing in response. ---")
             raise Exception("Hume config creation failed, ID missing.")

        log.info(f"--- Temporary Hume config created successfully. ID: {temp_config_id} ---")

        # --- 4. Connect to Hume EVI via WebSocket using the temporary config ID ---
        log.info(f"--- Attempting WebSocket connection to Hume EVI using config_id: {temp_config_id} ---")
        uri_with_key = f"{HUME_EVI_WS_URI}?apiKey={HUME_API_KEY}" # Check variable name, maybe HUME_EVI_WS_URI
        hume_websocket = await websockets.connect(uri_with_key)
        log.info("--- WebSocket connection to Hume EVI established. ---")

        active_connections[call_sid] = {
            "hume_ws": hume_websocket,
            "twilio_ws": None,
            "stream_sid": None,
            "resample_state": None,
            "temp_config_id": temp_config_id # Store for potential cleanup later
        }

        # --- 5. Send Initial *Audio* Settings via WebSocket ---
        # We use the temp_config_id, but still need to specify the audio format for the session
        initial_message = {
            "type": "session_settings",
            "config_id": temp_config_id, # Use the ID from the REST API call
            "audio": {
                "encoding": "linear16", # Hume expects 16-bit little-endian PCM input
                "sample_rate": 8000,    # Match Twilio's stream rate
                "channels": 1           # Mono audio
            }
            # NO "prompt", "voice", or "evi_version" needed here - they are in the config
        }
        await hume_websocket.send(json.dumps(initial_message))
        log.info(f"--- Sent session_settings using temp_config_id: {temp_config_id} ---")

        # Start the background task to listen for messages *from* Hume
        asyncio.create_task(listen_to_hume(call_sid))

    # --- Exception Handling for REST and WebSocket ---
    except requests.exceptions.RequestException as e:
        log.error(f"--- FAILED during Hume config creation or connection setup for {call_sid}: {e} ---")
        # Ensure cleanup if temp_config_id was created before failure
        # (Could add a check here to delete the config if temp_config_id exists)
        response_twiml = VoiceResponse(); response_twiml.say("Error setting up AI configuration."); response_twiml.hangup()
        return Response(content=str(response_twiml), media_type="text/xml")
    except websockets.exceptions.WebSocketException as e:
        log.error(f"--- FAILED during WebSocket connection for {call_sid}: {e} ---")
        # Ensure cleanup, potentially delete temp config
        await cleanup_connection(call_sid, "WebSocket connection failed") # cleanup might need temp_config_id now
        response_twiml = VoiceResponse(); response_twiml.say("Sorry, could not connect to the AI service."); response_twiml.hangup()
        return Response(content=str(response_twiml), media_type="text/xml")
    except Exception as e: # Catch other potential errors (like missing ID)
        log.error(f"--- UNEXPECTED ERROR in handle_incoming_call for {call_sid}: {type(e).__name__} - {e} ---")
        await cleanup_connection(call_sid, "Incoming call setup failed") # cleanup might need temp_config_id
        response_twiml = VoiceResponse(); response_twiml.say("An unexpected error occurred. Please try again later."); response_twiml.hangup()
        return Response(content=str(response_twiml), media_type="text/xml")

    # --- 6. Respond to Twilio to Start Streaming ---
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
    """
    Handles the WebSocket connection *from* Twilio.
    1. Receives audio chunks (mu-law) from Twilio.
    2. Transcodes audio to linear16 PCM.
    3. Forwards the PCM audio to Hume EVI.
    """
    try:
        await websocket.accept()
        log.info(f"--- Twilio WebSocket connected for CallSid: {call_sid} ---")

        # Retrieve connection details, ensure Hume WS exists
        connection_details = active_connections.get(call_sid)
        if not connection_details or not connection_details.get("hume_ws"):
            log.error(f"--- ERROR: Twilio WS connected, but Hume connection not found for CallSid: {call_sid}. Closing. ---")
            await websocket.close(code=1011, reason="Backend EVI connection missing")
            return

        # Store the Twilio WebSocket object
        connection_details["twilio_ws"] = websocket
        hume_ws = connection_details["hume_ws"]

        # Main loop processing messages from Twilio
        while True:
            message_str = await websocket.receive_text()
            data = json.loads(message_str)
            event = data.get("event")

            if event == "start":
                stream_sid = data["start"]["streamSid"]
                connection_details["stream_sid"] = stream_sid # Store for sending audio back
                log.info(f"--- Twilio 'start' message received, Stream SID: {stream_sid} ---")

            elif event == "media":
                # Received an audio chunk from the caller
                payload = data["media"]["payload"] # Base64 encoded mu-law audio

                # Ensure Hume WS is still open before processing/sending
                if hume_ws.closed:
                     log.warning(f"--- Twilio sent media, but Hume WS for {call_sid} is closed. Skipping. ---")
                     continue

                try:
                    # Decode and transcode: mu-law (Twilio) -> linear16 (Hume)
                    mulaw_bytes = base64.b64decode(payload)
                    pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2) # 2 = 16-bit output width
                    pcm_b64 = base64.b64encode(pcm_bytes).decode('utf-8')

                    # Forward to Hume
                    hume_message = { "type": "audio_input", "data": pcm_b64 }
                    await hume_ws.send(json.dumps(hume_message))

                except audioop.error as e:
                    log.error(f"    ERROR during Twilio->Hume audioop transcoding for {call_sid}: {e}")
                    # Decide whether to continue or break if transcoding fails repeatedly
                except Exception as e:
                    log.error(f"    ERROR processing Twilio media for {call_sid}: {type(e).__name__} - {e}")

            elif event == "stop":
                log.info(f"--- Twilio 'stop' message received for CallSid: {call_sid}. Ending stream handling. ---")
                break # Exit the loop normally

            elif event == "mark":
                log.info(f"--- Twilio 'mark' message received (Name: {data.get('mark', {}).get('name')}). Ignoring. ---")
                # Handle mark messages if needed for synchronization

            else:
                 log.warning(f"--- Received unknown event type from Twilio: {event} ---")


    except WebSocketDisconnect:
        log.warning(f"--- Twilio WebSocket disconnected unexpectedly for CallSid: {call_sid} ---")
    except websockets.exceptions.ConnectionClosedOK:
        log.info(f"--- Twilio WebSocket closed normally for CallSid: {call_sid} ---")
    except websockets.exceptions.ConnectionClosedError as e:
        log.warning(f"--- Twilio WebSocket closed with error for CallSid: {call_sid}: {e} ---")
    except Exception as e:
        log.error(f"--- UNEXPECTED ERROR in handle_twilio_audio_stream for {call_sid}: {type(e).__name__} - {e} ---")
    finally:
        # Ensure cleanup happens regardless of how the loop/connection ends
        await cleanup_connection(call_sid, "Twilio stream ended/disconnected")


# --- Background Task to Listen to Hume ---
async def listen_to_hume(call_sid: str):
    """
    Listens for messages *from* Hume EVI in a background task.
    1. Receives messages (including audio output as WAV).
    2. If audio, extracts PCM data from WAV.
    3. Resamples audio to 8kHz if necessary.
    4. Transcodes audio to mu-law.
    5. Forwards mu-law audio back to Twilio.
    """
    log.info(f"--- Started listening to Hume EVI for CallSid: {call_sid} ---")
    hume_ws = None
    resample_state = None # State for audioop.ratecv

    try:
        # Check connection exists at the start
        connection_details = active_connections.get(call_sid)
        if not connection_details or not connection_details.get("hume_ws"):
            log.error(f"--- listen_to_hume: Hume WS not found for {call_sid} at start. Task exiting. ---")
            return
        hume_ws = connection_details["hume_ws"]

        # Continuously listen for messages from Hume
        async for message_str in hume_ws:
            # Check if the connection still exists in our manager before processing
            # This handles cases where cleanup might happen due to Twilio disconnecting first
            connection_details = active_connections.get(call_sid)
            if not connection_details:
                 log.warning(f"--- listen_to_hume: Connection for {call_sid} disappeared. Exiting task. ---")
                 break # Exit loop if connection was cleaned up

            try:
                hume_data = json.loads(message_str)
                hume_type = hume_data.get("type")

                # Log non-audio events for context
                if hume_type != "audio_output":
                    log.info(f"--- Hume Event: {hume_type} ---")

                # --- Handle Audio Output from Hume ---
                if hume_type == "audio_output":
                    log.info("--- Hume Event: 'audio_output' ---")

                    # Ensure Twilio connection and streamSid are ready
                    twilio_ws = connection_details.get("twilio_ws")
                    stream_sid = connection_details.get("stream_sid")

                    if twilio_ws and stream_sid and not twilio_ws.client_state == websockets.protocol.State.CLOSED:
                        try:
                            # Decode the base64 WAV data
                            wav_b64 = hume_data["data"]
                            wav_bytes = base64.b64decode(wav_b64)

                            # Parse the WAV header and extract PCM data
                            pcm_bytes_hume = b''
                            input_rate_hume = 8000 # Default assumption
                            samp_width_hume = 2    # Default assumption (linear16)

                            with io.BytesIO(wav_bytes) as wav_file_like:
                                with wave.open(wav_file_like, 'rb') as wav_reader:
                                    n_channels = wav_reader.getnchannels()
                                    samp_width_hume = wav_reader.getsampwidth()
                                    input_rate_hume = wav_reader.getframerate()

                                    # Basic format validation
                                    if n_channels != 1:
                                        log.warning(f"Hume WAV has {n_channels} channels, expected 1. Trying to process...")
                                        # audioop might handle this, or might need stereo->mono conversion
                                    if samp_width_hume != 2:
                                        log.error(f"Hume WAV has sample width {samp_width_hume}, expected 2 (16-bit). Skipping chunk.")
                                        continue # Cannot transcode this easily

                                    pcm_bytes_hume = wav_reader.readframes(wav_reader.getnframes())

                            if not pcm_bytes_hume:
                                 log.warning("--- Could not read PCM data from Hume WAV chunk. Skipping. ---")
                                 continue

                            # Resample if Hume sent a different rate (e.g., 48k -> 8k)
                            output_rate_twilio = 8000
                            pcm_bytes_8k = pcm_bytes_hume
                            if input_rate_hume != output_rate_twilio:
                                log.info(f"    Resampling Hume audio from {input_rate_hume}Hz to {output_rate_twilio}Hz.")
                                pcm_bytes_8k, resample_state = audioop.ratecv(pcm_bytes_hume, samp_width_hume, 1, input_rate_hume, output_rate_twilio, resample_state)
                                # Store updated state back into connection details
                                connection_details["resample_state"] = resample_state


                            # Transcode the (potentially resampled) 8kHz PCM to mu-law for Twilio
                            mulaw_bytes = audioop.lin2ulaw(pcm_bytes_8k, samp_width_hume)
                            mulaw_b64 = base64.b64encode(mulaw_bytes).decode('utf-8')

                            # Format and send back to Twilio
                            twilio_media_message = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": { "payload": mulaw_b64 }
                            }
                            await twilio_ws.send_text(json.dumps(twilio_media_message))

                        except wave.Error as e:
                             log.error(f"    ERROR parsing Hume WAV data for {call_sid}: {e}")
                        except audioop.error as e:
                             log.error(f"    ERROR during Hume->Twilio audioop processing for {call_sid}: {e}")
                        except Exception as e:
                             log.error(f"    UNEXPECTED ERROR in Hume audio processing for {call_sid}: {type(e).__name__} - {e}")

                    else:
                        log.warning(f"--- Hume sent audio, but Twilio WS/stream_sid not ready for {call_sid}. Skipping. ---")

                # --- Handle Other Hume Message Types ---
                elif hume_type == "user_interruption":
                    log.info(f"--- Hume detected interruption for CallSid: {call_sid} ---")
                    # TODO: Implement interruption logic if needed (e.g., signal Twilio to stop playback)

                elif hume_type == "error":
                    log.error(f"--- Hume EVI Error (Full Message): {hume_data} ---")
                    # Decide if error is fatal and requires closing the connection
                    if hume_data.get('code', '').startswith('E'): # Assume 'E' codes are fatal
                         log.warning(f"--- Closing connection {call_sid} due to Hume fatal error. ---")
                         break # Exit the listen loop

                # Handle other types like 'user_message', 'assistant_message', 'tool_call' as needed

            except json.JSONDecodeError:
                log.warning(f"--- Could not decode JSON from Hume: {message_str[:100]}... ---")
            except Exception as e:
                log.error(f"--- UNEXPECTED ERROR processing Hume message for {call_sid}: {type(e).__name__} - {e} ---")
                # Consider breaking loop on unexpected errors?

    # --- Handle WebSocket Closure ---
    except websockets.exceptions.ConnectionClosedOK:
        log.info(f"--- Hume WebSocket closed normally for {call_sid}. ---")
    except websockets.exceptions.ConnectionClosedError as e:
        log.warning(f"--- Hume WebSocket closed with error for {call_sid}: {e} ---")
    except Exception as e: # Catch errors during the loop setup or iteration
        log.error(f"--- UNEXPECTED ERROR in listen_to_hume main loop for {call_sid}: {type(e).__name__} - {e} ---")
    finally:
        log.info(f"--- Stopped listening to Hume EVI for {call_sid}. Triggering cleanup. ---")
        # Ensure cleanup happens if Hume disconnects first or an error occurs
        await cleanup_connection(call_sid, "Hume listener stopped")

# --- Optional: Run directly for local testing (uvicorn command is preferred for deployment) ---
if __name__ == "__main__":
    import uvicorn
    # Use port from environment or default to 8080 for local dev
    port = int(os.environ.get("PORT", 8080))
    log.info(f"--- Starting Uvicorn server locally on port {port} ---")
    # Use reload=True for easier development
    uvicorn.run("app:app", host="0.0.0.0", port=port, workers=1, reload=True)