from flask import Flask, render_template
import sys # Used for a simple error message

# ---
# 1. IMPORT YOUR DUMMY DATA
# ---
# This line assumes "dummy_data.py" is in the same directory.
try:
    from dummy_data import DUMMY_PATIENT_DB
except ImportError:
    print("\n--- ERROR ---")
    print("Could not find the 'dummy_data.py' file.")
    print("Please make sure it's in the same folder as 'app.py'.")
    print("-------------\n")
    sys.exit(1) # Exit the app if data can't be found

# ---
# 2. CREATE YOUR FLASK APP
# ---
app = Flask(__name__)

# ---
# 3. ADD THE HELPER FUNCTIONS
# ---
# These are the functions we discussed. They find the data for us.

def get_patient_data(clinic_id, mrn):
    """
    Retrieves a specific patient's data from a specific clinic.
    """
    print(f"Attempting to fetch data for Clinic ID: {clinic_id}, MRN: {mrn}")
    
    # 1. Find the clinic by its ID
    clinic = DUMMY_PATIENT_DB.get(clinic_id)
    
    if not clinic:
        print(f"Error: Clinic with ID '{clinic_id}' not found.")
        return None  # Clinic not found
    
    # 2. Find the patient within that clinic's patient dictionary
    patient = clinic.get("patients", {}).get(mrn)
    
    if not patient:
        print(f"Error: Patient with MRN '{mrn}' not found in clinic '{clinic_id}'.")
        return None # Patient not found in this clinic

    print(f"Successfully found patient: {patient.get('name')}")
    # Return a copy to avoid any accidental changes to the original data
    return patient.copy()

def get_clinic_info(clinic_id):
    """
    Retrieves a clinic's general information (name, phone, etc.)
    excluding the full patient list.
    """
    clinic_data = DUMMY_PATIENT_DB.get(clinic_id)
    
    if not clinic_data:
        print(f"Error: Clinic with ID '{clinic_id}' not found.")
        return None
        
    # Create a copy and remove the sensitive/large patient list.
    info = clinic_data.copy()
    info.pop('patients', None) # Remove 'patients' key safely
    
    return info

# ---
# 4. DEFINE YOUR APP ROUTES (THE "PAGES")
# ---

@app.route('/')
def home():
    """
    A simple homepage to show that the app is working and give instructions.
    """
    return """
    <h1>Patient Portal App is Running!</h1>
    <p>This is the homepage. There's nothing here yet.</p>
    <p>Try visiting a patient-specific URL, like:</p>
    <ul>
        <li><a href="/patient/10001/ASTRO-001">/patient/10001/ASTRO-001</a> (Jose Altuve)</li>
        <li><a href="/patient/20002/ROCKET-A">/patient/20002/ROCKET-A</a> (Jalen Green)</li>
    </ul>
    """

@app.route('/patient/<string:clinic_id>/<string:mrn>')
def patient_portal(clinic_id, mrn):
    """
    This is the main patient page. It uses the URL to find the
    correct patient and clinic.
    """
    
    # Use our helper functions to get the data
    patient_data = get_patient_data(clinic_id, mrn)
    clinic_info = get_clinic_info(clinic_id)
    
    if not patient_data or not clinic_info:
        # Show an error if the patient or clinic isn't found
        return f"Error: Patient (MRN: {mrn}) or Clinic (ID: {clinic_id}) not found.", 404
    
    # If found, send the data to the 'index.html' template
    return render_template('index.html', 
                           patient=patient_data, 
                           clinic=clinic_info,
                           mrn=mrn) # Pass MRN too, as it's not in the patient dict

# ---
# 5. RUN THE APP
# ---
# This block allows you to run the app directly with "python app.py"
if __name__ == '__main__':
    # We set debug=True so the server reloads automatically
    # when you save the file.
    #
    # MODIFICATION: We change the port to 8080 so it doesn't
    # conflict with your other (Twilio/Hume) app, which can run on port 5000.
    app.run(debug=True, port=8080)

