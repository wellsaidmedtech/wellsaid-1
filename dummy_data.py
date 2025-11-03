import random

# --- Helper Function for Phone Numbers ---
def generate_phone():
    """Generates a random 10-digit US phone number."""
    return f"({random.randint(200, 999)}) {random.randint(200, 999)}-{random.randint(1000, 9999)}"

# --- Clinic 1: Astros Clinic (10001) ---

# Patient 1: Young, healthy male
patient_10001_001 = {
    "name": "Jose Altuve",
    "dob": "05/06/1990",
    "phone": generate_phone(),
    "encounters": {
        "10/15/2025": { "status": "signed", "type": "annual physical", "purpose": "routine check-up" },
        "04/10/2026": { "status": "scheduled", "type": "annual physical", "purpose": "routine check-up" }
    },
    "medical_conditions": [],
    "medications": [],
    "allergies": [],
    "procedures_history": []
}

# Patient 2: Elderly female
patient_10001_002 = {
    "name": "Betty White",
    "dob": "01/17/1922",
    "phone": generate_phone(),
    "encounters": {
        "09/20/2025": { "status": "signed", "type": "follow-up clinic visit", "purpose": "hypertension check" },
        "12/20/2025": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "hypertension check" },
        "09/25/2025": { "status": "scheduled", "type": "AI phone call", "purpose": "medication adherence" }
    },
    "medical_conditions": ["hypertension", "osteoporosis"],
    "medications": ["lisinopril 10 mg", "alendronate 70 mg weekly"],
    "allergies": ["codeine"],
    "procedures_history": ["R hip replacement (2018)"]
}

# Patient 3: Middle-aged male (from example)
patient_10001_003 = {
    "name": "Matt Damon",
    "dob": "10/08/1970",
    "phone": generate_phone(),
    "encounters": {
        "10/25/2025": { "status": "signed", "type": "follow-up clinic visit", "purpose": "post-op check" },
        "01/25/2026": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "routine follow-up" },
        "11/04/2025": { "status": "scheduled", "type": "AI phone call", "purpose": "post-op checkin" }
    },
    "medical_conditions": ["hyperlipidemia", "hypothyroidism"],
    "medications": ["atorvastatin 20 mg", "levothyroxine 100 mcg"],
    "allergies": ["penicillin"],
    "procedures_history": ["L knee replacement (2025)"]
}

# Patient 4: Middle-aged female
patient_10001_004 = {
    "name": "Sandra Bullock",
    "dob": "07/26/1964",
    "phone": generate_phone(),
    "encounters": {
        "08/05/2025": { "status": "signed", "type": "annual physical", "purpose": "routine check-up" },
        "08/10/2026": { "status": "scheduled", "type": "annual physical", "purpose": "routine check-up" }
    },
    "medical_conditions": ["migraines"],
    "medications": ["sumatriptan 50 mg (as needed)"],
    "allergies": ["sulfa drugs"],
    "procedures_history": ["C-section (2010)"]
}

# Patient 5: Female teenager
patient_10001_005 = {
    "name": "Zendaya Coleman",
    "dob": "09/01/2006",
    "phone": generate_phone(),
    "encounters": {
        "10/02/2025": { "status": "signed", "type": "acute visit", "purpose": "sore throat" },
        "11/15/2025": { "status": "scheduled", "type": "annual physical", "purpose": "routine check-up" }
    },
    "medical_conditions": ["acne"],
    "medications": ["tretinoin cream 0.025%"],
    "allergies": [],
    "procedures_history": []
}

# --- Clinic 2: Rockets Clinic (20002) ---

# Patient 1: Young, healthy male
patient_20002_00A = {
    "name": "Jalen Green",
    "dob": "02/09/2002",
    "phone": generate_phone(),
    "encounters": {
        "09/01/2025": { "status": "signed", "type": "annual physical", "purpose": "sports physical" },
        "09/01/2026": { "status": "scheduled", "type": "annual physical", "purpose": "sports physical" }
    },
    "medical_conditions": [],
    "medications": [],
    "allergies": [],
    "procedures_history": []
}

# Patient 2: Elderly male
patient_20002_00B = {
    "name": "Morgan Freeman",
    "dob": "06/01/1937",
    "phone": generate_phone(),
    "encounters": {
        "07/15/2025": { "status": "signed", "type": "follow-up clinic visit", "purpose": "diabetes check" },
        "01/15/2026": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "diabetes check" },
        "07/22/2025": { "status": "scheduled", "type": "AI phone call", "purpose": "medication adherence" }
    },
    "medical_conditions": ["type 2 diabetes", "atrial fibrillation"],
    "medications": ["metformin 1000 mg BID", "apixaban 5 mg BID"],
    "allergies": [],
    "procedures_history": ["cataract surgery (2019)"]
}

# Patient 3: Middle-aged male
patient_20002_00C = {
    "name": "Keanu Reeves",
    "dob": "09/02/1964",
    "phone": generate_phone(),
    "encounters": {
        "06/10/2025": { "status": "signed", "type": "annual physical", "purpose": "routine check-up" },
        "06/15/2026": { "status": "scheduled", "type": "annual physical", "purpose": "routine check-up" }
    },
    "medical_conditions": ["hypertension"],
    "medications": ["amlodipine 5 mg"],
    "allergies": [],
    "procedures_history": []
}

# Patient 4: Elderly female
patient_20002_00D = {
    "name": "Meryl Streep",
    "dob": "06/22/1949",
    "phone": generate_phone(),
    "encounters": {
        "10/01/2025": { "status": "signed", "type": "follow-up clinic visit", "purpose": "gastro reflux check" },
        "04/01/2026": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "gastro reflux check" }
    },
    "medical_conditions": ["GERD"],
    "medications": ["omeprazole 20 mg"],
    "allergies": ["aspirin"],
    "procedures_history": []
}

# Patient 5: Middle-aged female
patient_20002_00E = {
    "name": "Jennifer Aniston",
    "dob": "02/11/1969",
    "phone": generate_phone(),
    "encounters": {
        "05/20/2025": { "status": "signed", "type": "annual physical", "purpose": "routine check-up" },
        "05/20/2026": { "status": "scheduled", "type": "annual physical", "purpose": "routine check-up" }
    },
    "medical_conditions": ["seasonal allergies"],
    "medications": ["cetirizine 10 mg (as needed)"],
    "allergies": [],
    "procedures_history": ["rhinoplasty (2007)"]
}


# --- Data Structure for Seeding ---

DUMMY_PATIENT_DB = {
    "10001": {  # Astros Clinic
        "ASTRO-001": patient_10001_001,
        "ASTRO-002": patient_10001_002,
        "ASTRO-003": patient_10001_003,
        "ASTRO-004": patient_10001_004,
        "ASTRO-005": patient_10001_005,
    },
    "20002": {  # Rockets Clinic
        "ROCKET-A": patient_20002_00A,
        "ROCKET-B": patient_20002_00B,
        "ROCKET-C": patient_20002_00C,
        "ROCKET-D": patient_20002_00D,
        "ROCKBET-E": patient_20002_00E, # Typo intended for testing? Or should this be ROCKET-E?
    }
}

DUMMY_CLINIC_INFO = {
    "10001": {
        "clinic_name": "Astros Clinic",
        "clinic_address": "123 Main St, Houston, TX 77002",
        "clinic_phone": "(713) 555-0001"
    },
    "20002": {
        "clinic_name": "Rockets Clinic",
        "clinic_address": "456 Center Ct, Houston, TX 77002",
        "clinic_phone": "(713) 555-0002"
    }
}