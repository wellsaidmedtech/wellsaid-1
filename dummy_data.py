# This file contains the dummy patient and clinic database.

DUMMY_CLINIC_INFO = {
    "10001": {
        "clinic_id": "10001",
        "clinic_name": "Astros Clinic",
        "clinic_address": "501 Crawford St, Houston, TX 77002",
        "clinic_phone": "(713) 259-8000"
    },
    "20002": {
        "clinic_id": "20002",
        "clinic_name": "Rockets Clinic",
        "clinic_address": "1510 Polk St, Houston, TX 77002",
        "clinic_phone": "(713) 758-7200"
    },
    "30003": {
        "clinic_id": "30003",
        "clinic_name": "Texans Clinic",
        "clinic_address": "8400 Kirby Dr, Houston, TX 77054",
        "clinic_phone": "(832) 667-2002"
    }
}

DUMMY_PATIENT_DB = {
    # --- Astros Clinic Patients ---
    "10001": {
        "ASTRO-001": {
            "name": "Jose Altuve",
            "dob": "05/06/1990",
            "phone": "+18325551234", # <-- Patient's real phone
            "appointments": [
                {"date": "2025-10-28", "reason": "Annual Checkup"},
                {"date": "2025-11-15", "reason": "Follow-up"},
            ],
            "call_history": [
                {"date": "2025-10-20", "call_id": "CA123", "summary": "Discussed pre-checkup fasting."}
            ]
        },
        "ASTRO-002": {
            "name": "Alex Bregman",
            "dob": "03/30/1994",
            "phone": "+18325551235",
            "appointments": [
                {"date": "2025-11-01", "reason": "Flu Shot"},
            ],
            "call_history": []
        }
    },
    # --- Rockets Clinic Patients ---
    "20002": {
        "ROCKET-A": {
            "name": "Jalen Green",
            "dob": "02/09/2002",
            "phone": "+17135551236",
            "appointments": [
                {"date": "2025-11-05", "reason": "Sports Physical"},
            ],
            "call_history": []
        },
        "ROCKET-B": {
            "name": "Alperen Şengün",
            "dob": "07/25/2002",
            "phone": "+17135551237",
            "appointments": [],
            "call_history": [
                {"date": "2025-10-15", "call_id": "CA124", "summary": "Confirmed mailing address."}
            ]
        }
    },
    # --- Texans Clinic Patients ---
    "30003": {
        "TEXAN-X": {
            "name": "CJ Stroud",
            "dob": "10/03/2001",
            "phone": "+18325551238",
            "appointments": [
                {"date": "2025-11-10", "reason": "Pre-season physical"},
            ],
            "call_history": []
        }
    }
}

