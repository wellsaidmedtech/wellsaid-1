import random

def generate_phone():
    """Generates a random Houston-area phone number."""
    # Common Houston prefixes
    prefixes = ['713', '281', '832', '346']
    prefix = random.choice(prefixes)
    return f"+1 ({prefix}) {random.randint(100, 999)}-{random.randint(1000, 9999)}"

# --- CLINIC INFORMATION ---
# This remains the same, but is needed for the seeder
DUMMY_CLINIC_INFO = {
    "10001": {
        "clinic_name": "Astros Clinic",
        "clinic_address": "501 Crawford St, Houston, TX 77002",
        "clinic_phone": "+1 (713) 555-0001"
    },
    "20002": {
        "clinic_name": "Rockets Clinic",
        "clinic_address": "1510 Polk St, Houston, TX 77003",
        "clinic_phone": "+1 (713) 555-0002"
    }
}


# --- PATIENT DATABASE (NEW EHR-LIKE STRUCTURE) ---
DUMMY_PATIENT_DB = {
    # --- Astros Clinic ---
    "10001": {
        "ASTRO-001": {
            "name": "Jose Altuve",
            "dob": "05/06/1990",
            "phone": generate_phone(),
            "encounters": {
                "2025-10-15": { "status": "signed", "type": "annual physical", "purpose": "Routine Check-up" },
                "2026-04-10": { "status": "scheduled", "type": "annual physical", "purpose": "Routine Check-up" }
            },
            "medical_conditions": ["Seasonal allergies"],
            "medications": ["Loratadine 10 mg PRN"],
            "allergies": [],
            "procedures_history": []
        },
        "ASTRO-002": {
            "name": "Morgan Freeman",
            "dob": "06/01/1937",
            "phone": generate_phone(),
            "encounters": {
                "2025-09-20": { "status": "signed", "type": "chronic care visit", "purpose": "Hypertension/AFib check" },
                "2025-10-10": { "status": "signed", "type": "AI phone call", "purpose": "Check-in post-fall" },
                "2025-12-20": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "Chronic care visit" }
            },
            "medical_conditions": ["Hypertension", "Atrial Fibrillation", "Osteoarthritis", "History of Stroke (CVA) 2008"],
            "medications": ["Lisinopril 20 mg", "Eliquis 5 mg BID", "Tylenol 500 mg PRN for joint pain"],
            "allergies": ["Codeine"],
            "procedures_history": ["Coronary artery bypass graft (CABG) 2005"]
        },
        "ASTRO-003": {
            "name": "Judi Dench",
            "dob": "12/09/1934",
            "phone": generate_phone(),
            "encounters": {
                "2025-10-01": { "status": "signed", "type": "follow-up clinic visit", "purpose": "Macular degeneration check" },
                "2025-10-15": { "status": "scheduled", "type": "AI phone call", "purpose": "Blood pressure check" },
                "2026-01-10": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "Hypertension/Osteoporosis check" }
            },
            "medical_conditions": ["Macular Degeneration", "Hypertension", "Osteoporosis"],
            "medications": ["Amlodipine 10 mg", "Alendronate 70 mg weekly", "Lutein supplement"],
            "allergies": ["Sulfa drugs"],
            "procedures_history": ["Cataract surgery (bilateral) 2018", "R hip replacement 2016"]
        },
        "ASTRO-004": {
            "name": "Matt Damon",
            "dob": "10/08/1970",
            "phone": generate_phone(),
            "encounters": {
                "2025-10-25": { "status": "signed", "type": "follow-up clinic visit", "purpose": "Medication check" },
                "2026-01-25": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "Lab review" },
                "2025-11-04": { "status": "scheduled", "type": "AI phone call", "purpose": "Medication adherence" }
            },
            "medical_conditions": ["Hyperlipidemia", "Hypothyroidism"],
            "medications": ["Atorvastatin 20 mg", "Levothyroxine 100 mcg"],
            "allergies": ["Penicillin"],
            "procedures_history": ["L knee replacement"]
        },
        "ASTRO-005": {
            "name": "Jennifer Aniston",
            "dob": "02/11/1969",
            "phone": generate_phone(),
            "encounters": {
                "2025-08-15": { "status": "signed", "type": "annual wellness exam", "purpose": "Routine Check-up" },
                "2025-08-20": { "status": "signed", "type": "lab review (telehealth)", "purpose": "Review annual labs" },
                "2026-08-15": { "status": "scheduled", "type": "annual wellness exam", "purpose": "Routine Check-up" }
            },
            "medical_conditions": ["Migraines", "GERD"],
            "medications": ["Sumatriptan 50 mg PRN", "Omeprazole 20 mg daily"],
            "allergies": [],
            "procedures_history": ["Cholecystectomy (laparoscopic) 2011"]
        },
        "ASTRO-006": {
            "name": "Jenna Ortega",
            "dob": "09/27/2007",
            "phone": generate_phone(),
            "encounters": {
                "2025-09-05": { "status": "signed", "type": "sports physical", "purpose": "School requirement" },
                "2025-10-10": { "status": "signed", "type": "acute visit (ankle sprain)", "purpose": "Injury" }
            },
            "medical_conditions": ["Asthma (mild, intermittent)"],
            "medications": ["Albuterol inhaler 90 mcg PRN for wheezing"],
            "allergies": ["Peanuts"],
            "procedures_history": []
        }
    },
    # --- Rockets Clinic ---
    "20002": {
        "ROCKET-A": {
            "name": "Jalen Green",
            "dob": "02/09/2002",
            "phone": generate_phone(),
            "encounters": {
                "2025-10-01": { "status": "signed", "type": "pre-season physical", "purpose": "Team requirement" },
                "2026-01-15": { "status": "scheduled", "type": "follow-up (shoulder strain)", "purpose": "Injury follow-up" }
            },
            "medical_conditions": ["Osgood-Schlatter (history)"],
            "medications": ["Ibuprofen 400 mg PRN for knee pain"],
            "allergies": [],
            "procedures_history": []
        },
        "ROCKET-B": {
            "name": "Bill Murray",
            "dob": "09/21/1950",
            "phone": generate_phone(),
            "encounters": {
                "2025-10-12": { "status": "signed", "type": "chronic care visit", "purpose": "Diabetes/Gout check" },
                "2025-10-20": { "status": "scheduled", "type": "AI phone call", "purpose": "Glucose monitoring check-in" },
                "2026-02-10": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "Lab review" }
            },
            "medical_conditions": ["Type 2 Diabetes", "Gout", "BPH"],
            "medications": ["Metformin 1000 mg BID", "Allopurinol 300 mg daily", "Tamsulosin 0.4 mg daily"],
            "allergies": ["Shellfish"],
            "procedures_history": ["Colonoscopy 2020 (benign polyps removed)"]
        },
        "ROCKET-C": {
            "name": "Meryl Streep",
            "dob": "06/22/1949",
            "phone": generate_phone(),
            "encounters": {
                "2025-07-30": { "status": "signed", "type": "annual wellness exam", "purpose": "Routine Check-up" },
                "2025-10-30": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "Glaucoma check" }
            },
            "medical_conditions": ["Hypothyroidism", "GERD", "Glaucoma"],
            "medications": ["Levothyroxine 75 mcg", "Pantoprazole 40 mg", "Latanoprost eye drops"],
            "allergies": [],
            "procedures_history": ["Total hysterectomy 1995"]
        },
        "ROCKET-D": {
            "name": "Keanu Reeves",
            "dob": "09/02/1964",
            "phone": generate_phone(),
            "encounters": {
                "2025-09-15": { "status": "signed", "type": "acute visit (back pain)", "purpose": "Injury" },
                "2025-09-22": { "status": "signed", "type": "physical therapy referral", "purpose": "Back pain" },
                "2025-11-15": { "status": "scheduled", "type": "follow-up clinic visit", "purpose": "Physical therapy follow-up" }
            },
            "medical_conditions": ["Chronic low back pain", "Hypertension"],
            "medications": ["Losartan 50 mg", "Cyclobenzaprine 10 mg PRN"],
            "allergies": [],
            "procedures_history": []
        },
        "ROCKET-E": {
            "name": "Sandra Bullock",
            "dob": "07/26/1964",
            "phone": generate_phone(),
            "encounters": {
                "2025-05-10": { "status": "signed", "type": "annual wellness (pap/mammo)", "purpose": "Routine Check-up" },
                "2025-11-10": { "status": "scheduled", "type": "AI phone call", "purpose": "Post-procedure check-in" }
            },
            "medical_conditions": ["History of C-section", "Iron deficiency anemia"],
            "medications": ["Ferrous sulfate 325 mg daily"],
            "allergies": [],
            "procedures_history": ["C-section 2010"]
        },
        "ROCKET-F": {
            "name": "Zendaya",
            "dob": "09/01/2006",
            "phone": generate_phone(),
            "encounters": {
                "2025-10-02": { "status": "signed", "type": "acute visit (sore throat)", "purpose": "Illness" },
                "2025-10-20": { "status": "scheduled", "type": "AI phone call", "purpose": "Check symptoms" }
            },
            "medical_conditions": ["Eczema (mild)"],
            "medications": ["Triamcinolone cream 0.1% PRN for rash"],
            "allergies": [],
            "procedures_history": ["Wisdom teeth removal 2024"]
        }
    }
}

