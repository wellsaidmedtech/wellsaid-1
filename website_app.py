import os
import json
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv
from twilio.rest import Client
import firebase_admin
from firebase_admin import credentials, firestore

# --- Initialization ---
load_dotenv()

# Initialize Firebase
# Make sure your GOOGLE_APPLICATION_CREDENTIALS env var is set
# or that 'firebase-key.json' is in the same directory.
try:
    cred_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', 'firebase-key.json')
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Firebase Firestore client initialized successfully.")
except Exception as e:
    logging.error(f"Failed to initialize Firebase: {e}")
    db = None

# Initialize Flask App
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "a_very_secret_key_fallback")

# Initialize Twilio Client
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
RENDER_BACKEND_URL = os.environ.get("RENDER_BACKEND_URL")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- Helper Functions ---

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
                "id": doc.id,
                "name": clinic_data.get("name", "Unnamed Clinic")
            })
        return all_clinics
    except Exception as e:
        logging.error(f"Error fetching all clinics: {e}")
        return []

# --- Main App Routes ---

@app.route('/')
def dashboard():
    """Displays the main patient dashboard with filtering."""
    if not db:
        flash("Database connection not established.", "error")
        return render_template('dashboard.html', patients=[], clinics=[], render_url="")

    clinic_id_filter = request.args.get('clinic_id', '')
    patient_name_filter = request.args.get('patient_name', '').lower()
    
    patients_list = []
    try:
        clinics_ref = db.collection('clinics')
        
        # If a clinic is selected, only query its patients. Otherwise, query all.
        if clinic_id_filter:
            patient_collection_refs = [clinics_ref.document(clinic_id_filter).collection('patients')]
        else:
            all_clinic_docs = clinics_ref.stream()
            patient_collection_refs = [doc.reference.collection('patients') for doc in all_clinic_docs]
        
        for patient_ref in patient_collection_refs:
            for doc in patient_ref.stream():
                patient_data = doc.to_dict()
                patient_data['mrn'] = doc.id
                patient_data['clinic_id'] = patient_ref.parent.id # Get clinic_id from parent doc
                
                # Apply name filter
                if patient_name_filter and patient_name_filter not in patient_data.get('name', '').lower():
                    continue
                    
                patients_list.append(patient_data)
                
    except Exception as e:
        logging.error(f"Error fetching patients from Firestore: {e}")
        flash(f"Error fetching patients: {e}", "error")

    all_clinics = get_all_clinics()
    
    return render_template(
        'dashboard.html', 
        patients=patients_list, 
        clinics=all_clinics,
        selected_clinic=clinic_id_filter,
        patient_name=patient_name_filter,
        render_url=RENDER_BACKEND_URL
    )

@app.route('/patient/view/<clinic_id>/<mrn>')
def patient_detail(clinic_id, mrn):
    """Displays a single patient's detailed profile."""
    if not db:
        flash("Database connection not established.", "error")
        return redirect(url_for('dashboard'))
        
    try:
        doc_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)
        patient = doc_ref.get().to_dict()
        if patient:
            # Sort encounters by date (key)
            sorted_encounters = {}
            if 'encounters' in patient:
                sorted_keys = sorted(patient['encounters'].keys(), reverse=True)
                sorted_encounters = {key: patient['encounters'][key] for key in sorted_keys}
                patient['encounters'] = sorted_encounters
                
            return render_template('patient_detail.html', patient=patient, clinic_id=clinic_id, mrn=mrn)
        else:
            flash(f"Patient with MRN {mrn} not found in clinic {clinic_id}.", "error")
            return redirect(url_for('dashboard'))
    except Exception as e:
        logging.error(f"Error fetching patient {mrn}: {e}")
        flash(f"Error fetching patient: {e}", "error")
        return redirect(url_for('dashboard'))

# ... (Existing /patient/create route) ...
@app.route('/patient/create', methods=['GET', 'POST'])
def create_patient():
    """Serves the form to create a new patient and handles submission."""
    if not db:
        flash("Database connection not established.", "error")
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        try:
            data = request.form.to_dict(flat=False)
            
            clinic_id = data.get('clinic_id', [''])[0]
            mrn = data.get('mrn', [''])[0]
            
            if not clinic_id or not mrn:
                flash("Clinic ID and MRN are required.", "error")
                return redirect(request.url)

            patient_data = {
                "name": data.get('name', [''])[0],
                "dob": data.get('dob', [''])[0],
                "phone": data.get('phone', [''])[0],
                "medical_conditions": data.get('medical_conditions[]', []),
                "medications": data.get('medications[]', []),
                "allergies": data.get('allergies[]', []),
                "procedures_history": data.get('procedures_history[]', []),
                "encounters": {}
            }
            
            # Process dynamic encounters
            encounter_dates = data.get('encounter_date[]', [])
            encounter_types = data.get('encounter_type[]', [])
            encounter_statuses = data.get('encounter_status[]', [])
            encounter_purposes = data.get('encounter_purpose[]', [])
            
            for i, date in enumerate(encounter_dates):
                if date:
                    patient_data['encounters'][date] = {
                        "type": encounter_types[i] if i < len(encounter_types) else "",
                        "status": encounter_statuses[i] if i < len(encounter_statuses) else "",
                        "purpose": encounter_purposes[i] if i < len(encounter_purposes) else ""
                    }

            # Save to Firestore
            doc_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)
            doc_ref.set(patient_data)
            
            flash(f"Patient {patient_data['name']} created successfully!", "success")
            return redirect(url_for('patient_detail', clinic_id=clinic_id, mrn=mrn))
            
        except Exception as e:
            logging.error(f"Error creating patient: {e}")
            flash(f"Error creating patient: {e}", "error")
            return redirect(request.url)
    
    # GET request
    all_clinics = get_all_clinics()
    return render_template('create_patient.html', clinics=all_clinics)

# ... (Existing /patient/edit route) ...
@app.route('/patient/edit/<clinic_id>/<mrn>', methods=['GET', 'POST'])
def edit_patient(clinic_id, mrn):
    """Serves the form to edit an existing patient and handles submission."""
    if not db:
        flash("Database connection not established.", "error")
        return redirect(url_for('dashboard'))

    doc_ref = db.collection('clinics').document(clinic_id).collection('patients').document(mrn)

    if request.method == 'POST':
        try:
            data = request.form.to_dict(flat=False)
            
            patient_data = {
                "name": data.get('name', [''])[0],
                "dob": data.get('dob', [''])[0],
                "phone": data.get('phone', [''])[0],
                "medical_conditions": data.get('medical_conditions[]', []),
                "medications": data.get('medications[]', []),
                "allergies": data.get('allergies[]', []),
                "procedures_history": data.get('procedures_history[]', []),
                "encounters": {}
            }
            
            # Process dynamic encounters
            encounter_dates = data.get('encounter_date[]', [])
            encounter_types = data.get('encounter_type[]', [])
            encounter_statuses = data.get('encounter_status[]', [])
            encounter_purposes = data.get('encounter_purpose[]', [])
            
            for i, date in enumerate(encounter_dates):
                if date:
                    patient_data['encounters'][date] = {
                        "type": encounter_types[i] if i < len(encounter_types) else "",
                        "status": encounter_statuses[i] if i < len(encounter_statuses) else "",
                        "purpose": encounter_purposes[i] if i < len(encounter_purposes) else ""
                    }

            # Update in Firestore
            doc_ref.update(patient_data)
            
            flash(f"Patient {patient_data['name']} updated successfully!", "success")
            return redirect(url_for('patient_detail', clinic_id=clinic_id, mrn=mrn))
            
        except Exception as e:
            logging.error(f"Error updating patient {mrn}: {e}")
            flash(f"Error updating patient: {e}", "error")
            return redirect(request.url)

    # GET request: Fetch existing data
    try:
        patient = doc_ref.get().to_dict()
        if patient:
            # Sort encounters for display
            sorted_encounters = {}
            if 'encounters' in patient:
                sorted_keys = sorted(patient['encounters'].keys(), reverse=True)
                sorted_encounters = {key: patient['encounters'][key] for key in sorted_keys}
                patient['encounters'] = sorted_encounters
                
            return render_template('edit_patient.html', patient=patient, clinic_id=clinic_id, mrn=mrn)
        else:
            flash(f"Patient with MRN {mrn} not found.", "error")
            return redirect(url_for('dashboard'))
    except Exception as e:
        logging.error(f"Error fetching patient {mrn}: {e}")
        flash(f"Error fetching patient: {e}", "error")
        return redirect(url_for('dashboard'))

# ... (Existing /call route) ...
@app.route('/call')
def call_patient():
    """Initiates an outbound call to a patient via Twilio."""
    patient_phone = request.args.get('phone')
    override_phone = request.args.get('phone_override')
    mrn = request.args.get('mrn')
    clinic_id = request.args.get('clinic_id')

    if not all([patient_phone, mrn, clinic_id]):
        flash("Missing required information (phone, MRN, or Clinic ID).", "error")
        return redirect(url_for('dashboard'))

    # Use override number if provided, otherwise use patient's number
    target_phone = override_phone if override_phone else patient_phone
    
    if not RENDER_BACKEND_URL:
        flash("RENDER_BACKEND_URL is not configured in the environment.", "error")
        return redirect(url_for('dashboard'))

    try:
        # Construct the TwiML URL for our Render backend
        # This URL tells Twilio what to do *after* the patient picks up
        # It passes the MRN and Clinic ID so the AI agent has context
        twiml_url = f"{RENDER_BACKEND_URL}/twilio/incoming_call?mrn={mrn}&clinic_id={clinic_id}"

        logging.info(f"Initiating call to {target_phone} with TwiML URL: {twiml_url}")

        call = twilio_client.calls.create(
            to=target_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url,  # Twilio will fetch instructions from this URL
            method="POST"
        )
        
        flash(f"Calling {target_phone} (Call SID: {call.sid})", "success")
        
    except Exception as e:
        logging.error(f"Error initiating call: {e}")
        flash(f"Twilio call failed: {e}", "error")
        
    return redirect(url_for('dashboard'))

# -----------------------------------------------------------------
# NEW ROUTES FOR PROMPT EDITOR
# -----------------------------------------------------------------

@app.route('/admin/prompts')
def admin_prompts():
    """Displays the prompt editor page."""
    if not db:
        flash("Database connection not established.", "error")
        return redirect(url_for('dashboard'))
    
    try:
        prompt_ref = db.collection('prompt_library')
        
        # Define the prompts we want to edit
        prompt_ids = ['prompt_identity', 'prompt_rules', 'kb_medication_adherence', 'kb_post_op_checkin']
        
        prompts = {}
        for doc_id in prompt_ids:
            doc = prompt_ref.document(doc_id).get()
            if doc.exists:
                prompts[doc_id] = doc.to_dict().get('content', '')
            else:
                prompts[doc_id] = f"Error: Document '{doc_id}' not found in 'prompt_library'."

        return render_template('admin_prompts.html', prompts=prompts)
        
    except Exception as e:
        logging.error(f"Error fetching prompts: {e}")
        flash(f"Error fetching prompts: {e}", "error")
        return redirect(url_for('dashboard'))

@app.route('/admin/prompts/save', methods=['POST'])
def save_prompts():
    """Saves the updated prompts to Firestore."""
    if not db:
        flash("Database connection not established.", "error")
        return redirect(url_for('admin_prompts'))
        
    try:
        # Get all form data
        data = request.form
        
        prompt_ref = db.collection('prompt_library')
        batch = db.batch() # Use a batch for efficient updates

        for key, value in data.items():
            if key.startswith('prompt_'): # e.g., key is 'prompt_identity'
                doc_ref = prompt_ref.document(key)
                batch.update(doc_ref, {'content': value})
        
        batch.commit()
        
        flash("Prompts saved successfully!", "success")
        
    except Exception as e:
        logging.error(f"Error saving prompts: {e}")
        flash(f"Error saving prompts: {e}", "error")
        
    return redirect(url_for('admin_prompts'))


# --- Main Execution ---
if __name__ == "__main__":
    # Note: `debug=True` is great for development.
    # Turn it off in a production environment.
    app.run(debug=True, port=8080)

