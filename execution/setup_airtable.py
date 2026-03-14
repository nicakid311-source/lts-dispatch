"""
setup_airtable.py
-----------------
ONE-TIME SETUP SCRIPT — run this once to create all columns in the
Airtable Bookings table for the LTS WhatsApp Pipeline.

Run:  python setup_airtable.py

After running, verify the columns appear in your Airtable base,
then delete or archive this script — it is not part of the live pipeline.
"""

import os
import re
import requests
from dotenv import load_dotenv

load_dotenv()


def sanitize(msg: str) -> str:
    return re.sub(
        r'(key|token|sid|secret|password|authorization)[\s=:]+\S+',
        '[REDACTED]', str(msg), flags=re.IGNORECASE
    )


# ── Column definitions ────────────────────────────────────────────────────────
# Each dict maps to one Airtable field creation payload.
# Types: singleLineText, multilineText, number, currency, phoneNumber, dateTime

COLUMNS = [
    {"name": "Passenger Name",   "type": "singleLineText"},
    {"name": "Phone",            "type": "singleLineText"},
    {
        "name": "Pickup Date/Time",
        "type": "dateTime",
        "options": {
            "dateFormat":   {"name": "us"},
            "timeFormat":   {"name": "12hour"},
            "timeZone":     "America/New_York"
        }
    },
    {"name": "Pickup Location",  "type": "singleLineText"},
    {"name": "Dropoff Location", "type": "singleLineText"},
    {"name": "Vehicle Type",     "type": "singleLineText"},
    {"name": "Flight Number",    "type": "singleLineText"},
    {
        "name": "# Passengers",
        "type": "number",
        "options": {"precision": 0}
    },
    {
        "name": "Price",
        "type": "currency",
        "options": {"precision": 2, "symbol": "$"}
    },
    {"name": "Notes",            "type": "multilineText"},
    {"name": "Source Chat",      "type": "singleLineText"},
    {"name": "Raw Message",      "type": "multilineText"},
    {"name": "Status",           "type": "singleLineText"},
]


def create_columns():
    api_key  = os.getenv("AIRTABLE_API_KEY")
    base_id  = os.getenv("AIRTABLE_BASE_ID")
    table_id = "tblRRJKWEWQLmQf0l"   # confirmed table ID

    if not api_key or not base_id:
        raise EnvironmentError("AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json"
    }

    url = f"https://api.airtable.com/v0/meta/bases/{base_id}/tables/{table_id}/fields"

    created  = []
    skipped  = []
    failed   = []

    for col in COLUMNS:
        payload = {"name": col["name"], "type": col["type"]}
        if "options" in col:
            payload["options"] = col["options"]

        resp = requests.post(url, headers=headers, json=payload)

        if resp.status_code == 200:
            created.append(col["name"])
            print(f"  [OK]     {col['name']}")
        elif resp.status_code == 422 and "already exists" in resp.text.lower():
            skipped.append(col["name"])
            print(f"  [SKIP]   {col['name']} — already exists")
        else:
            # Sanitize response before printing — may contain token fragments
            safe_err = sanitize(resp.text)[:200]
            failed.append(col["name"])
            print(f"  [FAIL]   {col['name']} — {resp.status_code}: {safe_err}")

    print(f"\nDone. Created: {len(created)}  Skipped: {len(skipped)}  Failed: {len(failed)}")

    if failed:
        print(f"Failed columns: {failed}")
        print("Check column names or field types and retry.")


if __name__ == "__main__":
    print("Creating Airtable columns for LTS WhatsApp Pipeline...\n")
    try:
        create_columns()
    except Exception as e:
        print(f"Error: {sanitize(str(e))}")
        raise SystemExit(1)
