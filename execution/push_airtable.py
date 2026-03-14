"""
push_airtable.py
----------------
Inserts a single trip record into the Airtable Bookings table.

Input:  Structured trip JSON (from extract_trip.py, after Supabase confirms success)
Output: {"status": "inserted", "airtable_id": "..."}

Rules:
- Single record inserts only — no bulk operations, no deletes, no restructuring
- Only runs after Supabase push confirms success (caller responsibility)
- Never log PII fields
- FIELD_MAP below must match your actual Airtable column names exactly
  → Verify column names in Airtable and update FIELD_MAP before first production run
"""

import os
import re
import json
import argparse
from dotenv import load_dotenv
from pyairtable import Api

# Load environment variables from .env (read-only — never modify)
load_dotenv()


# ── Airtable field mapping ────────────────────────────────────────────────────
#
# IMPORTANT: These column names must match your Airtable table exactly.
# If a push fails with "Unknown field name", check this mapping first.
# Update the values (right side) to match your actual Airtable column names.
#
FIELD_MAP = {
    "passenger_name":  "Passenger Name",    # singleLineText
    "phone":           "Phone",             # singleLineText
    "pickup_datetime": "Pickup Date/Time",  # dateTime
    "pickup_location": "Pickup Location",   # singleLineText
    "dropoff_location":"Dropoff Location",  # singleLineText
    "vehicle_type":    "Vehicle Type",      # singleLineText
    "flight_number":   "Flight Number",     # singleLineText
    "num_passengers":  "# Passengers",      # number
    "affiliate":       "Affiliate",         # singleLineText
    "price":           "Price",             # singleLineText
    "notes":           "Notes",             # multilineText
    "source_chat":     "Source Chat",       # singleLineText
    "raw_message":     "Raw Message",       # multilineText
    "status":          "Status",            # singleLineText
    "submitted_by":    "Submitted By",      # singleLineText
}


# ── Sanitize error messages before any logging ───────────────────────────────

def sanitize(msg: str) -> str:
    """Strip credential patterns from error output before logging."""
    return re.sub(
        r'(key|token|sid|secret|password|authorization)[\s=:]+\S+',
        '[REDACTED]',
        str(msg),
        flags=re.IGNORECASE
    )


# ── Main insert function ──────────────────────────────────────────────────────

def push_airtable(trip: dict) -> dict:
    """
    Insert a single trip record into Airtable.

    Args:
        trip:  Structured trip dict from extract_trip.py

    Returns:
        dict with status and airtable_id
    """
    api_key  = os.getenv("AIRTABLE_API_KEY")
    base_id  = os.getenv("AIRTABLE_BASE_ID")
    table_name = os.getenv("AIRTABLE_TABLE_NAME")

    if not api_key or not base_id or not table_name:
        raise EnvironmentError(
            "AIRTABLE_API_KEY, AIRTABLE_BASE_ID, and AIRTABLE_TABLE_NAME must be set in .env"
        )

    # Add pipeline-managed status field to the trip dict before mapping
    trip_with_status = {**trip, "status": trip.get("status", "pending")}

    # Map trip fields to Airtable column names using FIELD_MAP
    # Filter out null/None values — Airtable rejects null field values
    record_fields = {}
    for trip_key, airtable_col in FIELD_MAP.items():
        value = trip_with_status.get(trip_key)
        if value is not None:
            record_fields[airtable_col] = value

    # Airtable Price field is Currency/Number — coerce from string to float
    if "Price" in record_fields:
        try:
            record_fields["Price"] = float(
                str(record_fields["Price"]).replace("$", "").replace(",", "").strip()
            )
        except (ValueError, TypeError):
            del record_fields["Price"]

    if not record_fields:
        raise ValueError("No valid fields to insert — trip dict appears empty")

    # Initialize Airtable client and target table
    api   = Api(api_key)
    table = api.table(base_id, table_name)

    try:
        # Single record insert only (per protected_resources.md)
        response = table.create(record_fields)
    except Exception as e:
        raise RuntimeError(f"Airtable insert failed: {sanitize(str(e))}") from None

    airtable_id = response.get("id")

    # Log success without PII — only log the Airtable record ID
    print(f"[push_airtable] Inserted record: id={airtable_id}")

    return {"status": "inserted", "airtable_id": airtable_id}


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Insert a trip record into Airtable."
    )
    parser.add_argument(
        "--trip-json",
        required=True,
        help="JSON string of extracted trip fields (from extract_trip.py)"
    )
    args = parser.parse_args()

    try:
        trip_data = json.loads(args.trip_json)
        result = push_airtable(trip_data)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2))
        raise SystemExit(1)
