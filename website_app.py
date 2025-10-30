import os
from flask import Flask, render_template, request, abort, redirect, url_for
from dummy_data import DUMMY_PATIENT_DB, DUMMY_CLINIC_INFO
# We will need these for the click-to-call
from twilio.rest import Client
from urllib.parse import urljoin

app = Flask(__name__)

# --- Twilio Configuration ---
# We'll get these from our local .env file
# Make sure to create a .env file locally with these values
# (This file should be in your .gitignore and NOT pushed to GitHub)
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')

# This is the PUBLIC URL of your Render app (the backend)
# e.g., "https://your-call-app.onrender.com"
# We need this so Twilio knows what URL to use for the call logic.
RENDER_BACKEND_URL = os.environ.get('RENDER_BACKEND_URL') 

# Initialize Twilio Client
# We only initialize if the keys are present, so the app doesn't crash
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
else:
    twilio_client = None
    print("WARNING: Twilio credentials not found. Click-to-call will not work.")
    print("Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in your .env file.")


# Helper function to get clinic info
def get_clinic_info(clinic_id):
    """Fetches clinic info from the dummy clinic dict."""
    return DUMMY_CLINIC_INFO.get(clinic_id)

# Helper function to get patient data
def get_patient_data(clinic_id, mrn):
    """Fetches a single patient's data."""
    clinic = DUMMY_PATIENT_DB.get(clinic_id)
    if not clinic:
        return None
    return clinic.get(mrn)

def get_all_patients_with_details():
    """
    Helper function to flatten the database for easier iteration in the template.
    Returns a list of (clinic_info, patient_info) tuples.
    """
    all_patients = []
    for clinic_id, patients in DUMMY_PATIENT_DB.items():
        clinic_info = get_clinic_info(clinic_id)
        if not clinic_info:
            continue
        
        for mrn, patient_data in patients.items():
            # Add clinic_id and mrn to the dictionaries for easy access
            patient_data['mrn'] = mrn
            clinic_info['clinic_id'] = clinic_id
            all_patients.append((clinic_info, patient_data))
    return all_patients

@app.route('/')
def dashboard():
    """
    Main dashboard page.
    Handles filtering by clinic and patient name.
    """
    # Get all patients
    patients_list = get_all_patients_with_details()
    
    # Get filter criteria from URL (e.g., /?clinic_id=10001&patient_name=Jose)
    clinic_filter = request.args.get('clinic_id')
    name_filter = request.args.get('patient_name')

    # Apply clinic filter
    if clinic_filter:
        patients_list = [
            (c, p) for (c, p) in patients_list if c['clinic_id'] == clinic_filter
        ]
    
    # Apply name filter (case-insensitive)
    if name_filter:
        patients_list = [
            (c, p) for (c, p) in patients_list if name_filter.lower() in p['name'].lower()
        ]
        
    # Get a unique list of clinics for the filter dropdown
    clinics = list(DUMMY_CLINIC_INFO.values())

    return render_template('dashboard.html', 
                           patients_list=patients_list, 
                           clinics=clinics,
                           filters_applied={'clinic_id': clinic_filter, 'patient_name': name_filter},
                           render_backend_url=RENDER_BACKEND_URL) # <-- ADD THIS


@app.route('/patient/<clinic_id>/<mrn>')
def patient_portal(clinic_id, mrn):
    """
    Shows the individual patient portal page (your existing route).
    """
    patient_data = get_patient_data(clinic_id, mrn)
    if not patient_data:
        abort(404, "Patient or Clinic not found.")
    
    clinic_info = get_clinic_info(clinic_id)
    if not clinic_info:
        abort(404, "Clinic not found.")

    return render_template('patient_detail.html', 
                           patient=patient_data, 
                           clinic=clinic_info,
                           mrn=mrn) # Pass mrn to the template

# --- This is our NEW "Click-to-Call" Route ---
# It's triggered by the "Call Patient" button
@app.route('/call/<clinic_id>/<mrn>')
def make_call(clinic_id, mrn):
    """
    Initiates an outbound call to the patient.
    This route is hit by the user's browser (from the dashboard).
    It then tells Twilio to:
    1. Call the patient (or the override number).
    2. When the patient answers, connect them to our *backend* Render app.
    """
    if not twilio_client or not RENDER_BACKEND_URL:
        abort(500, "Twilio client or RENDER_BACKEND_URL is not configured.")

    # 1. Get the patient's data
    patient = get_patient_data(clinic_id, mrn)
    if not patient:
        abort(404, "Patient not found.")

    # 2. Get the phone number to call
    # This comes from the ?phone_to_call=... query parameter
    # which our JavaScript (in dashboard.html) set for us.
    phone_to_call = request.args.get('phone_to_call')
    if not phone_to_call:
        # Fallback just in case, but JS should always provide it
        phone_to_call = patient['phone']

    # 3. Construct the URL for our *backend* (Render) app
    # This is the URL Twilio will call *after* the patient picks up.
    # We pass the MRN and Clinic ID, which fixes our "MRN: None" error!
    #
    # THIS IS THE FIX: We are changing "/twilio/outgoing_call" to
    # "/twilio/incoming_call" to match the route on call_app.py
    backend_webhook_url = urljoin(
        RENDER_BACKEND_URL, 
        f"/twilio/incoming_call?mrn={mrn}&clinic_id={clinic_id}"
    )

    try:
        # 4. Make the call!
        call = twilio_client.calls.create(
            to=phone_to_call,
            from_=TWILIO_PHONE_NUMBER,
            url=backend_webhook_url  # Tell Twilio what to do when the call connects
        )
        print(f"Initiated call to {phone_to_call} for MRN {mrn}. Call SID: {call.sid}")

    except Exception as e:
        print(f"Error making call: {e}")
        # In a real app, you'd show a proper error page
        return f"Error making call: {e}", 500

    # 5. Redirect back to the dashboard
    # This is a good user experience. The call is ringing in the background.
    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=True, host='0.0.0.0', port=port)
    