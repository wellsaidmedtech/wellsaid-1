import os
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv
from twilio.rest import Client
import firebase_admin
from firebase_admin import credentials, firestore

# --- Initialization ---
load_dotenv()

# Initialize Firebase
# This automatically uses the GOOGLE_APPLICATION_CREDENTIALS env var
try:
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not os.path.exists(cred_path):
        print(f"Warning: Firebase credentials file not found at {cred_path}. Using local 'firebase-key.json'.")
        cred_path = 'firebase-key.json' # Fallback for local dev
        if not os.path.exists(cred_path):
            raise FileNotFoundError("No Firebase key found. Please set GOOGLE_APPLICATION_CREDENTIALS or place 'firebase-key.json' in the root.")

    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Firebase Firestore client initialized successfully.")
except Exception as e:
    print(f"CRITICAL: Failed to initialize Firebase. App may not function. Error: {e}")
    db = None

# Initialize Twilio
twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID")
twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
twilio_phone = os.environ.get("TWILIO_PHONE_NUMBER")
render_backend_url = os.environ.get("RENDER_BACKEND_URL")
twilio_client = Client(twilio_sid, twilio_token)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "a_very_secret_default_key_fallback")

# --- Helper Functions ---

def get_all_clinics():
    """Fetches all clinic documents from Firestore."""
    if not db:
        return {}
    clinics_ref = db.collection('clinics')
    clinics = {}
    for doc in clinics_ref.stream():
        clinics[doc.id] = doc.to_dict().get('clinic_name', 'Unnamed Clinic')
    return clinics

def get_all_patients_with_details():
    """Fetches all patients from all clinics and their details."""
    if not db:
        return []
    
    all_patients = []
    clinics_ref = db.collection('clinics')
    for clinic_doc in clinics_ref.stream():
        clinic_id = clinic_doc.id
        clinic_name = clinic_doc.to_dict().get('clinic_name', 'Unnamed Clinic')
        
        patients_ref = clinic_doc.reference.collection('patients')
        for patient_doc in patients_ref.stream():
            patient_data = patient_doc.to_dict()
            patient_data['mrn'] = patient_doc.id
            patient_data['clinic_id'] = clinic_id
            patient_data['clinic_name'] = clinic_name
            all_patients.append(patient_data)
    
    # Sort by name
    all_patients.sort(key=lambda x: x.get('name', ''))
    return all_patients

def get_patient_data(clinic_id, mrn):
    """Fetches a single patient's data from Firestore."""
    if not db:
        return None
    try:
        doc_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict()
        else:
            return None
    except Exception as e:
        print(f"Error getting patient data: {e}")
        return None

def get_clinic_info(clinic_id):
    """Fetches a single clinic's info from Firestore."""
    if not db:
        return None
    try:
        doc_ref = db.collection('clinics').document(clinic_id)
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict()
        else:
            return None
    except Exception as e:
        print(f"Error getting clinic info: {e}")
        return None
        
def _process_form_data(form_data):
    """
    Helper function to process form data from create/edit forms
    into the correct EHR-style schema.
    """
    # Get list fields, filter out empty strings
    medications = [med.strip() for med in form_data.getlist('medication') if med.strip()]
    medical_conditions = [cond.strip() for cond in form_data.getlist('medical_condition') if cond.strip()]
    allergies = [alg.strip() for alg in form_data.getlist('allergy') if alg.strip()]
    procedures_history = [proc.strip() for proc in form_data.getlist('procedure') if proc.strip()]

    # Get encounter fields
    enc_dates = form_data.getlist('encounter_date')
    enc_types = form_data.getlist('encounter_type')
    enc_statuses = form_data.getlist('encounter_status')
    enc_purposes = form_data.getlist('encounter_purpose')

    # Rebuild the encounters dictionary
    encounters_dict = {}
    for date, type, status, purpose in zip(enc_dates, enc_types, enc_statuses, enc_purposes):
        if date: # Only add if a date is provided
            encounters_dict[date] = {
                "type": type.strip(),
                "status": status.strip(),
                "purpose": purpose.strip()
            }

    # Build the final patient object for Firestore
    patient_data = {
        "name": form_data.get('name'),
        "dob": form_data.get('dob'),
        "phone": form_data.get('phone'),
        "medications": medications,
        "medical_conditions": medical_conditions,
        "allergies": allergies,
        "procedures_history": procedures_history,
        "encounters": encounters_dict
    }
    return patient_data

# --- Main Routes ---

@app.route('/')
def dashboard():
    """Main dashboard, shows filterable list of all patients."""
    query_name = request.args.get('name', '').lower()
    query_clinic_id = request.args.get('clinic_id', '')

    all_patients = get_all_patients_with_details()
    
    # Apply filters
    filtered_patients = []
    for patient in all_patients:
        name_match = query_name in patient.get('name', '').lower()
        clinic_match = not query_clinic_id or patient.get('clinic_id') == query_clinic_id
        
        if name_match and clinic_match:
            filtered_patients.append(patient)

    return render_template(
        'dashboard.html',
        patients=filtered_patients,
        clinics=get_all_clinics(),
        query_name=query_name,
        query_clinic_id=query_clinic_id,
        render_backend_url=render_backend_url
    )

@app.route('/patient/<clinic_id>/<mrn>')
def patient_detail(clinic_id, mrn):
    """Shows the detailed portal for a single patient."""
    patient = get_patient_data(clinic_id, mrn)
    if not patient:
        flash("Error: Patient or Clinic not found.", "error")
        return redirect(url_for('dashboard'))
        
    clinic = get_clinic_info(clinic_id)
    
    # Sort encounters by date, newest first
    sorted_encounters = sorted(
        patient.get('encounters', {}).items(), 
        key=lambda item: item[0], 
        reverse=True
    )
    
    return render_template(
        'patient_detail.html',
        patient=patient,
        clinic=clinic,
        mrn=mrn,
        clinic_id=clinic_id,
        sorted_encounters=sorted_encounters
    )

@app.route('/create_patient', methods=['GET', 'POST'])
def create_patient():
    """Serves the form to create a new patient and handles submission."""
    if request.method == 'POST':
        try:
            clinic_id = request.form.get('clinic_id')
            mrn = request.form.get('mrn')
            
            if not clinic_id or not mrn:
                flash("Error: Clinic ID and MRN are required.", "error")
                return redirect(url_for('create_patient'))

            # Check if patient already exists
            if get_patient_data(clinic_id, mrn):
                flash(f"Error: Patient with MRN {mrn} already exists in this clinic.", "error")
                return redirect(url_for('create_patient'))

            # Process all form data into the new schema
            patient_data = _process_form_data(request.form)
            
            # Save to Firestore
            db.collection('clinics').document(clinic_id).collection('patients').document(mrn).set(patient_data)
            
            flash(f"Patient {patient_data['name']} created successfully!", "success")
            return redirect(url_for('dashboard'))
        
        except Exception as e:
            print(f"Error creating patient: {e}")
            flash(f"An error occurred: {e}", "error")
            return redirect(url_for('create_patient'))

    # GET request: Show the form
    return render_template('create_patient.html', clinics=get_all_clinics())


@app.route('/edit_patient/<clinic_id>/<mrn>', methods=['GET', 'POST'])
def edit_patient(clinic_id, mrn):
    """Serves the form to edit an existing patient and handles submission."""
    patient = get_patient_data(clinic_id, mrn)
    if not patient:
        flash("Error: Patient not found.", "error")
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        try:
            # Process all form data into the new schema
            patient_data = _process_form_data(request.form)
            
            # Update in Firestore
            db.collection('clinics').document(clinic_id).collection('patients').document(mrn).update(patient_data)
            
            flash(f"Patient {patient_data['name']} updated successfully!", "success")
            return redirect(url_for('patient_detail', clinic_id=clinic_id, mrn=mrn))
        
        except Exception as e:
            print(f"Error updating patient: {e}")
            flash(f"An error occurred: {e}", "error")
            return redirect(url_for('edit_patient', clinic_id=clinic_id, mrn=mrn))

    # GET request: Show the form, pre-filled with patient data
    
    # Sort encounters by date for display
    sorted_encounters = sorted(
        patient.get('encounters', {}).items(), 
        key=lambda item: item[0], 
        reverse=True
    )
    
    return render_template(
        'edit_patient.html', 
        patient=patient, 
        clinic_id=clinic_id, 
        mrn=mrn,
        sorted_encounters=sorted_encounters
    )


@app.route('/call', methods=['POST'])
def call_patient():
    """
    Places an outbound call to a patient (or demo number) via Twilio,
    connecting them to the Render backend for the AI call.
    """
    patient_mrn = request.form.get('mrn')
    clinic_id = request.form.get('clinic_id')
    patient_phone = request.form.get('phone')
    
    # Check for demo override number
    phone_override = request.form.get('phone_override')
    call_to_number = phone_override if phone_override else patient_phone
    
    if not all([patient_mrn, clinic_id, call_to_number, render_backend_url, twilio_phone]):
        flash("Error: Missing configuration for placing call.", "error")
        return redirect(url_for('dashboard'))

    try:
        # Construct the webhook URL for the backend
        # This is where we pass the patient context!
        webhook_url = f"{render_backend_url}/twilio/incoming_call?mrn={patient_mrn}&clinic_id={clinic_id}"

        print(f"Placing call to: {call_to_number}")
        print(f"Using webhook: {webhook_url}")

        call = twilio_client.calls.create(
            to=call_to_number,
            from_=twilio_phone,
            url=webhook_url,  # Twilio will POST to this URL when the call connects
            method="POST"
        )
        
        flash(f"Calling {call_to_number}... (Call SID: {call.sid})", "info")

    except Exception as e:
        print(f"Twilio call error: {e}")
        flash(f"Error placing call: {e}", "error")

    return redirect(url_for('dashboard'))

# --- Run Application ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)

