"""
This file contains the dummy patient database, structured by clinic.

New Structure:
{
    "clinic_id_1": {
        "clinic_name": "...",
        "clinic_phone": "...",
        "clinic_address": "...",
        "patients": {
            "mrn_A": { ... patient data ... },
            "mrn_B": { ... patient data ... }
        }
    },
    "clinic_id_2": { ... }
}
"""

DUMMY_PATIENT_DB = {
    "10001": {
        "clinic_name": "Astros Clinic",
        "clinic_phone": "713-123-4567",
        "clinic_address": "123 Main St, Houston, TX 77001",
        "patients": {
            "ASTRO-001": {
                "name": "Jose Altuve",
                "dob": "1990-05-06",
                "gender": "Male",
                "phone": "888-555-1212",
                "email": "jose.altuve@example.com",
                "appointments": [
                    {"date": "2025-11-10", "time": "10:00 AM", "doctor": "Dr. Smith", "reason": "Annual Checkup"},
                    {"date": "2025-11-20", "time": "02:30 PM", "doctor": "Dr. Jones", "reason": "Follow-up"},
                ]
            },
            "ASTRO-002": {
                "name": "Kyle Tucker",
                "dob": "1997-01-17",
                "gender": "Male",
                "phone": "888-555-1213",
                "email": "kyle.tucker@example.com",
                "appointments": [
                    {"date": "2025-11-15", "time": "11:00 AM", "doctor": "Dr. Smith", "reason": "Soreness in shoulder"},
                ]
            }
        }
    },
    "20002": {
        "clinic_name": "Rockets Wellness",
        "clinic_phone": "713-987-6543",
        "clinic_address": "456 Center Ct, Houston, TX 77002",
        "patients": {
            "ROCKET-A": {
                "name": "Jalen Green",
                "dob": "2002-02-09",
                "gender": "Male",
                "phone": "888-555-3434",
                "email": "jalen.green@example.com",
                "appointments": [
                    {"date": "2025-11-12", "time": "09:00 AM", "doctor": "Dr. Brown", "reason": "Knee Pain"},
                ]
            },
            "ROCKET-B": {
                "name": "Alperen Şengün",
                "dob": "2002-07-25",
                "gender": "Male",
                "phone": "888-555-3435",
                "email": "alperen.sengun@example.com",
                "appointments": [
                    {"date": "2025-11-18", "time": "03:00 PM", "doctor": "Dr. Brown", "reason": "Routine Physical"},
                ]
            }
        }
    }
}
