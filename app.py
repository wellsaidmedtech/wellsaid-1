import os
import requests # <-- Import the requests library
from flask import Flask, jsonify, request
# We are no longer importing HumeClient for this test

app = Flask(__name__)

# --- Our Fake Patient Database ---
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

# --- Your existing endpoints ---
@app.route("/")
def hello_world():
    return "My Healthcare AI Server is running!"

@app.route("/api/test")
def api_test():
    print("--- /api/test endpoint was called successfully! ---")
    return jsonify(message="This is a test message from the API.")

@app.route("/api/start-call/<patient_id>")
def start_call(patient_id):
    print(f"--- Server received request for patient_id: {patient_id} ---")
    patient_data = DUMMY_PATIENT_DB.get(patient_id)
    if patient_data:
        print(f"Found patient: {patient_data['name']}")
        return jsonify(patient_data)
    else:
        print("--- Patient ID not found in database ---")
        return jsonify(error="Patient not found"), 404

# --- Hume API Test Endpoint (Using Manual Request with Header) ---
@app.route("/api/test-hume")
def test_hume_connection():
    print("--- Testing Hume API connection using manual request and X-Hume-Api-Key header ---")

    # --- Step 1: Get API Key from environment ---
    api_key = os.environ.get("HUME_API_KEY")
    print(f"DEBUG: Python sees HUME_API_KEY: {'Yes' if api_key else 'No'}")
    # print(f"DEBUG: API Key starts with: {api_key[:5] if api_key else 'None'}") # Optional debug

    if not api_key:
        print("--- ERROR: Python cannot find HUME_API_KEY in environment. ---")
        return jsonify(
            status="error",
            message="Required Hume environment variable HUME_API_KEY not found.",
            details="Ensure 'export HUME_API_KEY=...' was run correctly in the same terminal."
        ), 500
    # -----------------------------------------------

    # --- Step 2: Prepare and Send the Request Manually ---
    hume_configs_url = "https://api.hume.ai/v0/evi/configs"
    headers = {
        "X-Hume-Api-Key": api_key,
        "Accept": "application/json; charset=utf-8" # Good practice to include Accept header
    }
    params = {
        "page_size": 5 # Limit the response size
    }

    try:
        print(f"--- Sending GET request to {hume_configs_url} with X-Hume-Api-Key header ---")
        response = requests.get(hume_configs_url, headers=headers, params=params)

        # Check for HTTP errors (4xx or 5xx)
        response.raise_for_status()

        # If successful, process the JSON response
        configs_data = response.json()
        # Extract config names (structure might vary slightly, check Hume docs if needed)
        config_names = [cfg.get('name', 'Unnamed Config') for cfg in configs_data.get('configs_page', [])]

        print(f"--- Hume connection SUCCESS (HTTP {response.status_code}). Found configs: {config_names} ---")
        return jsonify(
            status="success",
            message="Hume API connection successful using manual request.",
            available_configs=config_names,
            raw_response=configs_data # Optionally return the raw data
        )

    except requests.exceptions.HTTPError as http_err:
        # Handle specific HTTP errors (like 401, 403)
        status_code = http_err.response.status_code
        print(f"--- Hume connection FAILED (HTTP {status_code}): {http_err} ---")
        print(f"--- Response Body: {http_err.response.text} ---") # Log the actual error response from Hume
        error_message = f"HTTP Error {status_code}: {http_err}. Check API Key and permissions."
        if status_code == 401:
            error_message = "Authentication failed (401 Unauthorized). Double-check the HUME_API_KEY environment variable is correct."
        elif status_code == 403:
             error_message = "Authorization failed (403 Forbidden). API Key might lack permissions."

        return jsonify(
            status="error",
            message="Failed to connect to Hume API via manual request.",
            error_details=error_message,
            raw_error_body=http_err.response.text # Include Hume's error message
        ), status_code

    except requests.exceptions.RequestException as req_err:
        # Handle other request errors (network issues, etc.)
        print(f"--- Hume connection FAILED (Request Exception): {req_err} ---")
        return jsonify(
            status="error",
            message="Failed to send request to Hume API.",
            error_details=str(req_err)
        ), 500

    except Exception as e:
        # Catch any other unexpected errors
        print(f"--- An unexpected error occurred: {e} ---")
        return jsonify(
            status="error",
            message="An unexpected error occurred.",
            error_details=str(e)
        ), 500

# --- NEW: Twilio Incoming Call Webhook ---
@app.route("/twilio/incoming_call", methods=['GET', 'POST'])
def handle_incoming_call():
    """Handles incoming calls from Twilio."""
    print("-" * 30)
    print(">>> Twilio Incoming Call Webhook Received <<<")

    # Get data Twilio sends about the call
    from_number = request.values.get('From', None)
    to_number = request.values.get('To', None)
    call_sid = request.values.get('CallSid', None)

    print(f"  Call From: {from_number}")
    print(f"  Call To: {to_number}")
    print(f"  Call SID: {call_sid}")

    # Create a TwiML response
    response = VoiceResponse()
    response.say("Thank you for calling. We will connect you shortly.", voice='alice')
    # We could add <Dial>, <Record>, <Gather> verbs here later
    # For now, we'll just hang up after the message.
    response.hangup()

    print("--- Responding to Twilio with TwiML ---")
    print(str(response))
    print("-" * 30)

    # Return the TwiML response to Twilio
    return str(response), 200, {'Content-Type': 'text/xml'}

# ----------------------------------------------------

if __name__ == "__main__":
    # Use the port Render assigns via environment variable, default to 5000 if not set
    port = int(os.environ.get('PORT', 5000))
    # Remove debug=True for production
    app.run(host='0.0.0.0', port=port)