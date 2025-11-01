import os
import logging
import re
from datetime import datetime
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, render_template, request, redirect, url_for, flash, session
from twilio.rest import Client

# --- 1. Initialization & Config ---

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] [%(funcName)s:%(lineno)d] %(message)s')

# --- 2. Flask App & Firebase Initialization ---

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "a_very_secret_development_key")

# Initialize Firebase
try:
    # This is the official env variable the Firebase Admin SDK looks for.
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    
    if not cred_path:
        logging.warning("GOOGLE_APPLICATION_CREDENTIALS env variable not set. Using 'firebase-key.json' as default.")
        # Set a default path if not provided, common for local dev
        cred_path = "firebase-key.json"
        
    if not os.path.exists(cred_path):
        logging.error(f"Firebase credentials file not found at: {cred_path}. Please ensure 'firebase-key.json' is in the root directory.")
        db = None
    else:
        logging.info(f"Initializing Firebase with key: {cred_path}")
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logging.info("Firebase Firestore client initialized successfully.")
        
except Exception as e:
    logging.error(f"CRITICAL: Failed to initialize Firebase. App will not run. Error: {e}", exc_info=True)
    db = None # Ensure db is None if init fails

# --- 3. Twilio Client Initialization ---
try:
    twilio_client = Client(os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN"))
    TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
    RENDER_BACKEND_URL = os.environ.get("RENDER_BACKEND_URL")
    if not all([TWILIO_PHONE_NUMBER, RENDER_BACKEND_URL]):
        logging.warning("Twilio env variables (TWILIO_PHONE_NUMBER or RENDER_BACKEND_URL) are missing!")
except Exception as e:
    logging.error(f"Failed to initialize Twilio client: {e}")
    twilio_client = None

# --- 4. Helper Functions ---

def get_all_clinics():
    """Fetches all clinic documents from Firestore."""
    if not db:
        return []
    try:
        clinics_ref = db.collection('clinics')
        all_clinics = []
        for doc in clinics_ref.stream():
            clinic_data = doc.to_dict()
            all_clinics.append({
                'id': doc.id,
                'name': clinic_data.get('name', 'Unnamed Clinic')
            })
        logging.info(f"Fetched {len(all_clinics)} clinics.")
        return all_clinics
    except Exception as e:
        logging.error(f"Error fetching clinics: {e}", exc_info=True)
        return []

def find_next_action(patient):
    """Finds the soonest scheduled action for a patient."""
    next_action = {'date': '9999-99-99', 'purpose': 'N/A'}
    if 'encounters' in patient and patient['encounters']:
        today = datetime.now().strftime('%Y-%m-%d')
        for date, enc in patient['encounters'].items():
            # Check if encounter is valid, scheduled, on or after today, and sooner than the current next_action
            if (enc and isinstance(enc, dict) and 
                enc.get('status') == 'scheduled' and 
                date >= today and 
                date < next_action['date']):
                next_action = {'date': date, 'purpose': enc.get('purpose', 'N/A')}
    
    if next_action['date'] == '9999-99-99':
        return {'date': None, 'purpose': 'None Scheduled'}
    return next_action

def validate_patient_form(form_data):
    """
    Validates the form data for creating/editing a patient.
    Returns (is_valid, error_message)
    """
    # Regex for YYYY-MM-DD (more standard)
    dob_regex = r'^(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$'
    # Simple North American phone regex (basic)
    phone_regex = r'^\+?1?\s*\(?([2-9][0-8][0-9])\)?\s*-?([2-9][0-9]{2})\s*-?([0-9]{4})$'
    # Simple text validation (allows letters, numbers, spaces, hyphens, commas, periods)
    text_field_regex = r'^[a-zA-Z0-9\s,-.]+$'

    if not all([form_data.get('name'), form_data.get('mrn'), form_data.get('clinic_id'), form_data.get('dob')]):
        return False, "Name, MRN, Clinic, and DOB are all required fields."

    if not re.match(dob_regex, form_data.get('dob', '')):
        return False, "Date of Birth must be in YYYY-MM-DD format."
    
    if form_data.get('phone') and not re.match(phone_regex, form_data.get('phone', '')):
        return False, "Phone number format is not valid. (e.g., +1 (555) 123-4567)"

    # Validate dynamic fields to prevent nonsense data
    list_fields_to_check = ['medications', 'allergies', 'medical_conditions', 'procedures_history']
    for field_name in list_fields_to_check:
        for item in form_data.getlist(field_name + '[]'):
            if item and not re.match(text_field_regex, item):
                return False, f"Invalid entry in {field_name.replace('_', ' ').title()}: '{item}'. Only letters, numbers, spaces, and hyphens are allowed."

    return True, ""


# --- 5. Flask Routes ---

@app.route('/')
def dashboard():
    """Displays the main patient dashboard with filters."""
    if not db:
        flash("CRITICAL: Firestore database is not connected. Check server logs.", "danger")
        return render_template('dashboard.html', patients=[], clinics=[], patient_name="", selected_clinic="", render_url=None)
        
    try:
        clinics = get_all_clinics()
        
        # Get filter query params
        patient_name_filter = request.args.get('patient_name', '').lower().strip()
        clinic_id_filter = request.args.get('clinic_id', '').strip()

        patients_list = []
        
        # Base query
        clinic_refs = []
        if clinic_id_filter:
            # Filter by a specific clinic
            clinic_refs = [db.collection('clinics').document(clinic_id_filter)]
        else:
            # Get all clinics
            clinic_refs = db.collection('clinics').stream()

        for clinic in clinic_refs:
            patient_collection_ref = clinic.reference.collection('patients')
            for doc in patient_collection_ref.stream():
                patient_data = doc.to_dict()
                patient_data['mrn'] = doc.id
                patient_data['clinic_id'] = clinic.id
                
                # Apply name filter
                if patient_name_filter and patient_name_filter not in patient_data.get('name', '').lower():
                    continue
                
                # Add next action logic
                patient_data['next_action'] = find_next_action(patient_data)
                patients_list.append(patient_data)
                
    except Exception as e:
        logging.error(f"Error fetching dashboard data: {e}", exc_info=True)
        flash(f"An error occurred while fetching patient data: {e}", "danger")
        patients_list = []
        clinics = get_all_clinics() # Try to get clinics even if patient query fails

    return render_template(
        'dashboard.html',
        patients=patients_list,
        clinics=clinics,
        patient_name=request.args.get('patient_name', ''),
        selected_clinic=request.args.get('clinic_id', ''),
        render_url=RENDER_BACKEND_URL
    )

@app.route('/patient/<string:clinic_id>/<string:mrn>')
def patient_detail(clinic_id, mrn):
    """Displays the detailed view for a single patient."""
    if not db:
        flash("Database not connected.", "danger")
        return redirect(url_for('dashboard'))
        
    try:
        patient_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)
        patient = patient_ref.get()
        if not patient.exists:
            flash(f"Patient with MRN {mrn} not found in clinic {clinic_id}.", "danger")
            return redirect(url_for('dashboard'))
            
        patient_data = patient.to_dict()
        patient_data['mrn'] = mrn
        patient_data['clinic_id'] = clinic_id
        
        # Get clinic name
        clinic_doc = db.collection('clinics').document(clinic_id).get()
        clinic_name = clinic_doc.to_dict().get('name', 'Unknown Clinic')
        
        return render_template('patient_detail.html', patient=patient_data, clinic_name=clinic_name)
        
    except Exception as e:
        logging.error(f"Error fetching patient detail for {clinic_id}/{mrn}: {e}", exc_info=True)
        flash(f"An error occurred: {e}", "danger")
        return redirect(url_for('dashboard'))

@app.route('/call')
def call_patient():
    """
    Initiates a 'click-to-call' outbound call via Twilio.
    Passes MRN and ClinicID to the backend as query parameters.
    """
    if not twilio_client:
        flash("Twilio client is not configured. Check server logs.", "danger")
        return redirect(url_for('dashboard'))

    patient_phone = request.args.get('phone')
    mrn = request.args.get('mrn')
    clinic_id = request.args.get('clinic_id')
    phone_override = request.args.get('phone_override')
    
    # Use override if present, otherwise use patient's number
    call_to_number = phone_override if phone_override else patient_phone
    
    if not all([patient_phone, mrn, clinic_id, call_to_number]):
        flash("Missing required information (phone, MRN, or clinic) to place call.", "danger")
        return redirect(url_for('dashboard'))
        
    try:
        # This is the URL our Render app is listening on
        # We pass MRN and ClinicID as query params
        webhook_url = f"{RENDER_BACKEND_URL}/twilio/incoming_call?mrn={mrn}&clinic_id={clinic_id}"

        logging.info(f"Initiating call to {call_to_number} with webhook {webhook_url}")

        call = twilio_client.calls.create(
            to=call_to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url, # Twilio will call this URL *after* the patient picks up
            method="POST"
        )
        
        flash(f"Initiating call to {call_to_number}... (SID: {call.sid})", "success")
        
    except Exception as e:
        logging.error(f"Error initiating Twilio call: {e}", exc_info=True)
        flash(f"Twilio call failed: {e}", "danger")
        
    return redirect(url_for('dashboard'))

@app.route('/patient/create', methods=['GET', 'POST'])
def create_patient():
    """
    GET: Displays the form to create a new patient.
    POST: Validates and saves the new patient to Firestore.
    """
    if not db:
        flash("Database not connected.", "danger")
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        try:
            form_data = request.form
            
            # --- NEW: Validation Step ---
            is_valid, error_message = validate_patient_form(form_data)
            if not is_valid:
                flash(f"Validation Error: {error_message}", "danger")
                # Return the form, pre-filled with the (invalid) data
                return render_template('create_patient.html', clinics=get_all_clinics(), patient=form_data), 400

            clinic_id = form_data['clinic_id']
            mrn = form_data['mrn'].strip().upper()

            # --- NEW: Check for duplicate MRN ---
            patient_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)
            if patient_ref.get().exists:
                flash(f"Error: A patient with MRN {mrn} already exists in this clinic.", "danger")
                return render_template('create_patient.html', clinics=get_all_clinics(), patient=form_data), 400

            # Process valid data
            patient_data = {
                'name': form_data['name'].strip(),
                'dob': form_data['dob'].strip(),
                'phone': form_data['phone'].strip(),
                'clinic_id': clinic_id, # Storing clinic_id for reference
                'mrn': mrn, # Storing mrn for reference
                # Process dynamic list fields (filter out empty strings)
                'medications': list(filter(None, form_data.getlist('medications[]'))),
                'allergies': list(filter(None, form_data.getlist('allergies[]'))),
                'medical_conditions': list(filter(None, form_data.getlist('medical_conditions[]'))),
                'procedures_history': list(filter(None, form_data.getlist('procedures_history[]'))),
                'encounters': {} # Start with empty encounters
            }
            
            # Process dynamic encounter fields
            encounter_dates = form_data.getlist('encounter_date[]')
            encounter_types = form_data.getlist('encounter_type[]')
            encounter_statuses = form_data.getlist('encounter_status[]')
            encounter_purposes = form_data.getlist('encounter_purpose[]')

            for i in range(len(encounter_dates)):
                if encounter_dates[i]: # Only add if date is specified
                    patient_data['encounters'][encounter_dates[i]] = {
                        'type': encounter_types[i],
                        'status': encounter_statuses[i],
                        'purpose': encounter_purposes[i]
                    }

            # Save to Firestore
            patient_ref.set(patient_data)
            
            flash(f"Patient {patient_data['name']} created successfully!", "success")
            return redirect(url_for('dashboard'))

        except Exception as e:
            logging.error(f"Error creating patient: {e}", exc_info=True)
            flash(f"An error occurred while creating the patient: {e}", "danger")
            # Pass form data back to template to re-populate
            return render_template('create_patient.html', clinics=get_all_clinics(), patient=request.form)

    # GET request
    return render_template('create_patient.html', clinics=get_all_clinics(), patient={})

@app.route('/patient/edit/<string:clinic_id>/<string:mrn>', methods=['GET', 'POST'])
def edit_patient(clinic_id, mrn):
    """
    GET: Displays the form to edit an existing patient, pre-filled.
    POST: Validates and updates the patient data in Firestore.
    """
    if not db:
        flash("Database not connected.", "danger")
        return redirect(url_for('dashboard'))
        
    patient_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)

    if request.method == 'POST':
        try:
            form_data = request.form

            # --- NEW: Validation Step ---
            is_valid, error_message = validate_patient_form(form_data)
            if not is_valid:
                flash(f"Validation Error: {error_message}", "danger")
                # Re-load the edit page with the *original* data + the bad form data for user to fix
                patient_data = patient_ref.get().to_dict()
                patient_data['mrn'] = mrn
                patient_data['clinic_id'] = clinic_id
                # Overlay the bad form data so the user can see their mistakes
                patient_data.update(form_data) 
                return render_template('edit_patient.html', clinics=get_all_clinics(), patient=patient_data), 400

            # Process valid data
            patient_data = {
                'name': form_data['name'].strip(),
                'dob': form_data['dob'].strip(),
                'phone': form_data['phone'].strip(),
                'clinic_id': form_data['clinic_id'],
                'mrn': form_data['mrn'],
                # Process dynamic list fields (filter out empty strings)
                'medications': list(filter(None, form_data.getlist('medications[]'))),
                'allergies': list(filter(None, form_data.getlist('allergies[]'))),
                'medical_conditions': list(filter(None, form_data.getlist('medical_conditions[]'))),
                'procedures_history': list(filter(None, form_data.getlist('procedures_history[]'))),
                'encounters': {}
            }
            
            # Process dynamic encounter fields
            encounter_dates = form_data.getlist('encounter_date[]')
            encounter_types = form_data.getlist('encounter_type[]')
            encounter_statuses = form_data.getlist('encounter_status[]')
            encounter_purposes = form_data.getlist('encounter_purpose[]')

            for i in range(len(encounter_dates)):
                if encounter_dates[i]:
                    patient_data['encounters'][encounter_dates[i]] = {
                        'type': encounter_types[i],
                        'status': encounter_statuses[i],
                        'purpose': encounter_purposes[i]
                    }

            # --- Handle Clinic/MRN Change ---
            # if clinic_id or MRN changed, we must delete the old doc and create a new one.
            if clinic_id != form_data['clinic_id'] or mrn != form_data['mrn']:
                # New reference
                new_clinic_id = form_data['clinic_id']
                new_mrn = form_data['mrn'].strip().upper()
                new_patient_ref = db.collection('clinics').document(new_clinic_id).collection('patients').document(new_mrn)
                
                if new_patient_ref.get().exists:
                    flash(f"Error: A patient with MRN {new_mrn} already exists in clinic {new_clinic_id}.", "danger")
                    patient_data = patient_ref.get().to_dict() # Get original data
                    patient_data.update(form_data) # Add user's bad edits
                    return render_template('edit_patient.html', clinics=get_all_clinics(), patient=patient_data), 400
                
                # Create the new patient record
                new_patient_ref.set(patient_data)
                # Delete the old patient record
                patient_ref.delete()
                logging.info(f"Patient moved from {clinic_id}/{mrn} to {new_clinic_id}/{new_mrn}")
            else:
                # Simple update, no MRN/clinic change
                patient_ref.update(patient_data)
            
            flash(f"Patient {patient_data['name']} updated successfully!", "success")
            return redirect(url_for('patient_detail', clinic_id=patient_data['clinic_id'], mrn=patient_data['mrn']))

        except Exception as e:
            logging.error(f"Error updating patient {clinic_id}/{mrn}: {e}", exc_info=True)
            flash(f"An error occurred while updating the patient: {e}", "danger")
            # Pass form data back to template to re-populate
            patient_data = request.form.to_dict(flat=False) # Get form data as dict
            patient_data['mrn'] = mrn # Add identifiers back
            patient_data['clinic_id'] = clinic_id
            return render_template('edit_patient.html', clinics=get_all_clinics(), patient=patient_data)

    # GET request: Show the pre-filled form
    try:
        patient = patient_ref.get()
        if not patient.exists:
            flash(f"Patient with MRN {mrn} not found in clinic {clinic_id}.", "danger")
            return redirect(url_for('dashboard'))
            
        patient_data = patient.to_dict()
        patient_data['mrn'] = mrn
        patient_data['clinic_id'] = clinic_id
        
        return render_template('edit_patient.html', clinics=get_all_clinics(), patient=patient_data)
    except Exception as e:
        logging.error(f"Error fetching patient for edit {clinic_id}/{mrn}: {e}", exc_info=True)
        flash(f"An error occurred: {e}", "danger")
        return redirect(url_for('dashboard'))

@app.route('/admin/prompts', methods=['GET', 'POST'])
def admin_prompts():
    """
    GET: Displays an editor for all prompts in the 'prompt_library' collection.
    POST: Updates the prompts in Firestore.
    """
    if not db:
        flash("Database not connected.", "danger")
        return redirect(url_for('dashboard'))
    
    prompt_ref = db.collection('prompt_library')

    if request.method == 'POST':
        try:
            for key, value in request.form.items():
                # We expect keys like 'prompt_identity', 'prompt_rules', etc.
                prompt_ref.document(key).update({'content': value})
            flash("Prompts updated successfully!", "success")
        except Exception as e:
            logging.error(f"Error updating prompts: {e}", exc_info=True)
            flash(f"An error occurred while updating prompts: {e}", "danger")
        
        return redirect(url_for('admin_prompts'))

    # GET request: Fetch all prompts
    try:
        prompts = {}
        for doc in prompt_ref.stream():
            prompts[doc.id] = doc.to_dict().get('content', '')
        
        # Ensure all core prompts exist, even if empty, so the page doesn't crash
        core_prompts = ['prompt_identity', 'prompt_rules', 'kb_medication_adherence', 'kb_post_op_checkin']
        for p in core_prompts:
            if p not in prompts:
                prompts[p] = f"WARNING: Prompt '{p}' not found in Firestore. Please create it."
                
        return render_template('admin_prompts.html', prompts=prompts)
    except Exception as e:
        logging.error(f"Error fetching prompts: {e}", exc_info=True)
        flash(f"An error occurred while fetching prompts: {e}", "danger")
        return render_template('admin_prompts.html', prompts={})


# --- 6. Run Application ---

if __name__ == '__main__':
    # This check ensures we're using the local key file for local dev
    if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        logging.info("Setting default GOOGLE_APPLICATION_CREDENTIALS to 'firebase-key.json' for local dev.")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "firebase-key.json"
        
    app.run(host='127.0.0.1', port=8080, debug=True)

