import os
import re
import logging
from flask import Flask, render_template, request, redirect, url_for, flash
from twilio.rest import Client
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime

# --- Configuration & Initialization ---

# 1. Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# 2. Load Environment Variables
load_dotenv()

# 3. Initialize Firebase
try:
    cred = credentials.Certificate('firebase-key.json')
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Firebase Firestore client initialized successfully.")
except Exception as e:
    logging.error(f"Failed to initialize Firebase: {e}")
    db = None

# 4. Initialize Twilio Client
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
RENDER_BACKEND_URL = os.getenv("RENDER_BACKEND_URL")
if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, RENDER_BACKEND_URL]):
    logging.warning("One or more environment variables (Twilio, Render URL) are missing.")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# 5. Initialize Flask App
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "a_very_secret_key_fallback")

# --- Helper Functions ---

def get_all_clinics():
    """Fetches all clinic documents from Firestore."""
    if not db:
        return {}
    try:
        clinics_ref = db.collection('clinics')
        clinics = clinics_ref.stream()
        clinic_data = {clinic.id: clinic.to_dict() for clinic in clinics}
        return clinic_data
    except Exception as e:
        logging.error(f"Error fetching all clinics: {e}")
        return {}

def get_all_patients(clinic_filter=None, name_filter=None):
    """Fetches all patients, with optional filters."""
    if not db:
        return []
    
    patients_list = []
    try:
        clinics_ref = db.collection('clinics')
        
        if clinic_filter:
            clinic_refs = [clinics_ref.document(clinic_filter)]
        else:
            clinic_refs = clinics_ref.stream()

        for clinic in clinic_refs:
            clinic_id = clinic.id
            clinic_name = clinic.to_dict().get("clinic_name", "Unknown Clinic")
            patients_ref = clinic.reference.collection('patients')
            
            if name_filter:
                # Firestore text search is complex; for this app, we filter post-fetch
                patients_stream = patients_ref.stream()
            else:
                patients_stream = patients_ref.stream()

            for patient in patients_stream:
                patient_data = patient.to_dict()
                
                # Apply name filter post-fetch
                if name_filter and name_filter.lower() not in patient_data.get("name", "").lower():
                    continue
                    
                patient_data['mrn'] = patient.id
                patient_data['clinic_id'] = clinic_id
                patient_data['clinic_name'] = clinic_name
                
                # Find next scheduled AI action
                next_action = get_next_ai_action(patient_data.get("encounters", {}))
                patient_data['next_action'] = next_action

                patients_list.append(patient_data)
                
        # Sort by name
        patients_list.sort(key=lambda x: x.get('name', ''))
        return patients_list
        
    except Exception as e:
        logging.error(f"Error fetching patients: {e}")
        return []

def get_next_ai_action(encounters):
    """Finds the soonest scheduled AI call from an encounters dictionary."""
    next_action = {"date": "N/A", "purpose": "N/A"}
    soonest_date = None
    
    if not encounters or not isinstance(encounters, dict):
        return next_action

    today = datetime.now().date()

    for date_str, encounter in encounters.items():
        if encounter.get("status") == "scheduled" and "AI" in encounter.get("type", ""):
            try:
                # --- FIX: Standardize on MM/DD/YYYY ---
                enc_date = datetime.strptime(date_str, '%m/%d/%Y').date()
                if enc_date >= today:
                    if soonest_date is None or enc_date < soonest_date:
                        soonest_date = enc_date
                        next_action["date"] = date_str
                        next_action["purpose"] = encounter.get("purpose", "N/A")
            except ValueError:
                # Ignore invalid date formats
                continue
    return next_action

# --- FIX: Standardize on MM/DD/YYYY ---
DATE_REGEX = re.compile(r'^(0[1-9]|1[0-2])/(0[1-9]|[12][0-9]|3[01])/\d{4}$')
PHONE_REGEX = re.compile(r'^\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}$')
TEXT_FIELD_REGEX = re.compile(r"^[a-zA-Z0-9 \-,'.()&]+$")

def is_valid_date_format(date_str):
    """Checks if a date string is in MM/DD/YYYY format."""
    if not DATE_REGEX.match(date_str):
        return False
    try:
        # --- FIX: Standardize on MM/DD/YYYY ---
        datetime.strptime(date_str, '%m/%d/%Y')
        return True
    except ValueError:
        return False

def validate_list_field(field_list):
    """Checks a list of strings for simple text validation."""
    for item in field_list:
        if not TEXT_FIELD_REGEX.match(item):
            return item  # Return the invalid item
    return None

def parse_form_data(form_data):
    """Parses flat form data into a structured patient dictionary."""
    patient_data = {
        "name": form_data.get("name", "").strip(),
        "dob": form_data.get("dob", "").strip(),
        "phone": form_data.get("phone", "").strip(),
        # --- FIX: Was incorrectly reading 'medications[]' for medical_conditions ---
        "medical_conditions": [v.strip() for v in form_data.getlist("medical_conditions[]") if v.strip()],
        "medications": [v.strip() for v in form_data.getlist("medications[]") if v.strip()],
        "allergies": [v.strip() for v in form_data.getlist("allergies[]") if v.strip()],
        "procedures_history": [v.strip() for v in form_data.getlist("procedures[]") if v.strip()],
        "encounters": {}
    }
    
    # Parse encounters
    encounter_dates = form_data.getlist("enc_date[]")
    encounter_types = form_data.getlist("enc_type[]")
    encounter_statuses = form_data.getlist("enc_status[]")
    encounter_purposes = form_data.getlist("enc_purpose[]")

    for i, date_str in enumerate(encounter_dates):
        if date_str.strip():
            date_key = date_str.strip()
            patient_data["encounters"][date_key] = {
                "type": encounter_types[i].strip(),
                "status": encounter_statuses[i].strip(),
                "purpose": encounter_purposes[i].strip()
            }
    return patient_data

def validate_patient_data(patient_data, mrn, clinic_id, is_new=False):
    """Validates patient data. Returns True if valid, or a flash message if invalid."""
    
    # Check required fields
    if not all([patient_data['name'], patient_data['dob'], mrn, clinic_id]):
        return "Name, MRN, Clinic, and DOB are all required fields."
    
    # Check MRN/Clinic duplicate for new patients
    if is_new and db:
        doc_ref = db.collection(f"clinics/{clinic_id}/patients").document(mrn)
        if doc_ref.get().exists:
            return f"A patient with MRN {mrn} already exists at this clinic."

    # --- FIX: Standardize on MM/DD/YYYY ---
    if not is_valid_date_format(patient_data['dob']):
        return "Date of Birth must be in MM/DD/YYYY format."
        
    if not PHONE_REGEX.match(patient_data['phone']):
        return "Phone number format is not valid (e.g., (123) 456-7890)."

    # Validate lists
    for field_name, field_list in [
        ("Medications", patient_data["medications"]),
        ("Medical Conditions", patient_data["medical_conditions"]),
        ("Allergies", patient_data["allergies"]),
        ("Procedures History", patient_data["procedures_history"])
    ]:
        invalid_item = validate_list_field(field_list)
        if invalid_item:
            return f"Invalid entry in {field_name}: '{invalid_item}'. Only letters, numbers, and basic punctuation are allowed."
    
    # Validate encounters
    for date_str, enc in patient_data["encounters"].items():
        # --- FIX: Standardize on MM/DD/YYYY ---
        if not is_valid_date_format(date_str):
            return f"Encounter date '{date_str}' is not in the required MM/DD/YYYY format."
        if not all([enc['type'], enc['status'], enc['purpose']]):
            return f"Encounter on {date_str} is missing a type, status, or purpose."
    
    return True

# --- Routes ---

@app.route('/')
def dashboard():
    """Main dashboard page."""
    clinic_filter = request.args.get('clinic_filter', '')
    name_filter = request.args.get('name_filter', '')
    
    all_clinics = get_all_clinics()
    patients = get_all_patients(clinic_filter, name_filter)
    
    return render_template(
        'dashboard.html',
        patients=patients,
        all_clinics=all_clinics,
        clinic_filter=clinic_filter,
        name_filter=name_filter,
        render_url=RENDER_BACKEND_URL
    )

@app.route('/patient/<clinic_id>/<mrn>')
def patient_detail(clinic_id, mrn):
    """Shows a single patient's detail page."""
    if not db:
        flash("Database not connected.", "error")
        return redirect(url_for('dashboard'))
        
    try:
        doc_ref = db.collection(f"clinics/{clinic_id}/patients").document(mrn)
        patient = doc_ref.get().to_dict()
        if not patient:
            flash("Patient not found.", "error")
            return redirect(url_for('dashboard'))
            
        clinic_doc = db.collection('clinics').document(clinic_id).get()
        clinic_name = clinic_doc.to_dict().get("clinic_name", "Unknown Clinic")
        
        # Sort encounters by date (newest first)
        sorted_encounters = sorted(
            patient.get("encounters", {}).items(), 
            key=lambda item: datetime.strptime(item[0], '%m/%d/%Y'), 
            reverse=True
        )
        
        return render_template(
            'patient_detail.html',
            patient=patient,
            mrn=mrn,
            clinic_id=clinic_id,
            clinic_name=clinic_name,
            sorted_encounters=sorted_encounters
        )
    except Exception as e:
        logging.error(f"Error fetching patient detail for {clinic_id}/{mrn}: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('dashboard'))

@app.route('/patient/create', methods=['GET', 'POST'])
def create_patient():
    """Page to create a new patient."""
    if not db:
        flash("Database not connected.", "error")
        return redirect(url_for('dashboard'))

    all_clinics = get_all_clinics()

    if request.method == 'POST':
        mrn = request.form.get("mrn", "").strip()
        clinic_id = request.form.get("clinic_id", "").strip()
        patient_data = parse_form_data(request.form)
        
        validation_result = validate_patient_data(patient_data, mrn, clinic_id, is_new=True)
        
        if validation_result is not True:
            flash(validation_result, "error")
            # Re-render form with user's entered data
            return render_template('create_patient.html', all_clinics=all_clinics, patient=patient_data, mrn=mrn, selected_clinic=clinic_id), 400
        
        try:
            # Data is valid, save to Firestore
            doc_ref = db.collection(f"clinics/{clinic_id}/patients").document(mrn)
            doc_ref.set(patient_data)
            flash(f"Patient {patient_data['name']} created successfully!", "success")
            return redirect(url_for('patient_detail', clinic_id=clinic_id, mrn=mrn))
            
        except Exception as e:
            logging.error(f"Error creating patient: {e}")
            flash(f"An error occurred: {e}", "error")
            return render_template('create_patient.html', all_clinics=all_clinics, patient=patient_data, mrn=mrn, selected_clinic=clinic_id), 500

    # GET request
    return render_template('create_patient.html', all_clinics=all_clinics, patient={}, mrn="", selected_clinic=None)

@app.route('/patient/edit/<clinic_id>/<mrn>', methods=['GET', 'POST'])
def edit_patient(clinic_id, mrn):
    """Page to edit an existing patient."""
    if not db:
        flash("Database not connected.", "error")
        return redirect(url_for('dashboard'))
        
    doc_ref = db.collection(f"clinics/{clinic_id}/patients").document(mrn)
    all_clinics = get_all_clinics()

    if request.method == 'POST':
        # New clinic_id/MRN can be part of the form
        new_mrn = request.form.get("mrn", "").strip()
        new_clinic_id = request.form.get("clinic_id", "").strip()
        patient_data = parse_form_data(request.form)
        
        # We pass the *new* MRN/Clinic for validation
        validation_result = validate_patient_data(patient_data, new_mrn, new_clinic_id, is_new=False)
        
        if validation_result is not True:
            flash(validation_result, "error")
            # Re-render form with user's entered data
            return render_template('edit_patient.html', all_clinics=all_clinics, patient=patient_data, mrn=new_mrn, clinic_id=new_clinic_id), 400
        
        try:
            # If MRN or Clinic ID changed, we must delete the old doc and create a new one
            if new_mrn != mrn or new_clinic_id != clinic_id:
                # Create the new doc first
                new_doc_ref = db.collection(f"clinics/{new_clinic_id}/patients").document(new_mrn)
                new_doc_ref.set(patient_data)
                # Delete the old doc
                doc_ref.delete()
                logging.info(f"Patient moved from {clinic_id}/{mrn} to {new_clinic_id}/{new_mrn}")
            else:
                # Just update the existing doc
                doc_ref.set(patient_data)
                
            flash(f"Patient {patient_data['name']} updated successfully!", "success")
            return redirect(url_for('patient_detail', clinic_id=new_clinic_id, mrn=new_mrn))
            
        except Exception as e:
            logging.error(f"Error updating patient {clinic_id}/{mrn}: {e}")
            flash(f"An error occurred: {e}", "error")
            return render_template('edit_patient.html', all_clinics=all_clinics, patient=patient_data, mrn=new_mrn, clinic_id=new_clinic_id), 500

    # GET request
    try:
        patient = doc_ref.get().to_dict()
        if not patient:
            flash("Patient not found.", "error")
            return redirect(url_for('dashboard'))
            
        return render_template(
            'edit_patient.html',
            patient=patient,
            mrn=mrn,
            clinic_id=clinic_id,
            all_clinics=all_clinics
        )
    except Exception as e:
        logging.error(f"Error fetching patient for edit {clinic_id}/{mrn}: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('dashboard'))

@app.route('/call', methods=['POST'])
def call_patient():
    """Initiates an outbound call via Twilio."""
    clinic_id = request.form.get('clinic_id')
    mrn = request.form.get('mrn')
    patient_phone = request.form.get('patient_phone')
    override_phone = request.form.get('phone_override', '').strip()
    
    call_to_number = override_phone if (override_phone and PHONE_REGEX.match(override_phone)) else patient_phone
    
    if not call_to_number:
        flash("Invalid phone number.", "error")
        return redirect(url_for('dashboard'))
        
    if not RENDER_BACKEND_URL:
        flash("Backend URL is not configured.", "error")
        return redirect(url_for('dashboard'))

    # This is the URL on our Render app that Twilio will call *after* it connects to the patient
    # We pass the MRN and Clinic ID so the backend knows who it's talking to
    twilio_webhook_url = f"{RENDER_BACKEND_URL}/twilio/incoming_call?mrn={mrn}&clinic_id={clinic_id}"

    try:
        call = twilio_client.calls.create(
            to=call_to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=twilio_webhook_url,  # Twilio will fetch TwiML from this URL
            method='POST'
        )
        flash(f"Calling {call_to_number}... (Call SID: {call.sid})", "success")
        logging.info(f"Initiated call to {call_to_number} for {clinic_id}/{mrn}. Call SID: {call.sid}")
        
    except Exception as e:
        logging.error(f"Twilio call failed: {e}")
        # Parse Twilio-specific error if possible
        error_message = str(e)
        if "Authenticate" in error_message:
            error_message = "Twilio authentication failed. Check your Account SID and Auth Token in the .env file."
        elif "permission to send" in error_message:
            error_message = "Twilio permission error. Is the 'To' number verified in your Twilio trial account?"
            
        flash(f"Twilio call failed: {error_message}", "error")

    return redirect(url_for('dashboard'))

@app.route('/admin/prompts', methods=['GET', 'POST'])
def admin_prompts():
    """Page to view and edit AI prompts in Firestore."""
    if not db:
        flash("Database not connected.", "error")
        return redirect(url_for('dashboard'))

    prompt_collection = db.collection('prompt_library')
    
    if request.method == 'POST':
        try:
            for doc_id, content in request.form.items():
                prompt_collection.document(doc_id).set({'content': content})
            flash("Prompts updated successfully!", "success")
        except Exception as e:
            logging.error(f"Error updating prompts: {e}")
            flash(f"An error occurred: {e}", "error")
        return redirect(url_for('admin_prompts'))

    # GET request
    try:
        prompts = {}
        prompt_docs = prompt_collection.stream()
        for doc in prompt_docs:
            prompts[doc.id] = doc.to_dict().get('content', '')
            
        # Ensure all required prompts are present, even if empty
        required_prompts = ['prompt_identity', 'prompt_rules', 'kb_medication_adherence', 'kb_post_op_checkin']
        for req_prompt in required_prompts:
            if req_prompt not in prompts:
                prompts[req_prompt] = ""
                
        return render_template('admin_prompts.html', prompts=prompts)
    except Exception as e:
        logging.error(f"Error fetching prompts: {e}")
        flash(f"An error occurred: {e}", "error")
        return redirect(url_for('dashboard'))

# --- Main Execution ---

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=True, host='0.0.0.0', port=port)

