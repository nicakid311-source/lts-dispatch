"""
push_supabase.py
----------------
Inserts a new booking record into the Supabase `bookings` table.

Input:  Structured trip JSON (from extract_trip.py)
Output: {"status": "inserted", "booking_id": "..."} or {"status": "duplicate", "skipped": true}

Rules:
- INSERT ONLY — never update, overwrite, or delete existing records
- Always check for duplicate raw_message before inserting
- Never log PII fields (passenger_name, phone, pickup_location, etc.)
- `bookings` is a PROTECTED table — requires confirmation before any DELETE or bulk UPDATE
"""

import os
import re
import json
import argparse
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables from .env (read-only — never modify)
load_dotenv()


# ── Sanitize error messages before any logging ───────────────────────────────

def sanitize(msg: str) -> str:
    """Strip credential patterns from error output before logging."""
    return re.sub(
        r'(key|token|sid|secret|password|authorization|https?://[^\s]+)',
        '[REDACTED]',
        str(msg),
        flags=re.IGNORECASE
    )


# ── Supabase client setup ─────────────────────────────────────────────────────

def get_client() -> Client:
    """Initialize and return the Supabase client."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise EnvironmentError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(url, key)


# ── Duplicate check ───────────────────────────────────────────────────────────

def check_duplicate(client: Client, raw_message: str) -> bool:
    """
    Query bookings table for an existing record with the same raw_message.
    Returns True if a duplicate exists.
    """
    try:
        response = (
            client.table("bookings")
            .select("id")
            .eq("raw_message", raw_message)
            .limit(1)
            .execute()
        )
        return len(response.data) > 0
    except Exception as e:
        raise RuntimeError(f"Duplicate check failed: {sanitize(str(e))}") from None


# ── Main insert function ──────────────────────────────────────────────────────

def push_supabase(trip: dict, check_only: bool = False) -> dict:
    """
    Insert a trip booking into Supabase.

    Args:
        trip:        Structured trip dict from extract_trip.py
        check_only:  If True, only check for duplicates — do not insert

    Returns:
        dict with status and booking_id (or duplicate/skipped flags)
    """
    client = get_client()

    raw_message = trip.get("raw_message", "")

    # Always check for duplicates before any write (per platform_sync directive)
    if check_duplicate(client, raw_message):
        return {"status": "duplicate", "skipped": True}

    if check_only:
        return {"status": "no_duplicate", "skipped": False}

    # Build insert payload — all trip fields + pipeline metadata
    # Strip keys with None values; Supabase accepts null but we're explicit here
    payload = {
        "passenger_name":  trip.get("passenger_name"),
        "phone":           trip.get("phone"),
        "pickup_datetime": trip.get("pickup_datetime"),
        "pickup_location": trip.get("pickup_location"),
        "dropoff_location": trip.get("dropoff_location"),
        "vehicle_type":    trip.get("vehicle_type"),
        "flight_number":   trip.get("flight_number"),
        "num_passengers":  trip.get("num_passengers"),
        "price":           trip.get("price"),
        "notes":           trip.get("notes"),
        "source_chat":     trip.get("source_chat"),
        "raw_message":     raw_message,
        "status":          "pending",   # Default status per directive
        "created_at":      datetime.now(timezone.utc).isoformat(),
    }

    try:
        response = client.table("bookings").insert(payload).execute()
    except Exception as e:
        raise RuntimeError(f"Supabase insert failed: {sanitize(str(e))}") from None

    if not response.data:
        raise RuntimeError("Supabase insert returned no data — record may not have been created")

    booking_id = response.data[0].get("id")

    # Log success without PII — only log the booking ID
    print(f"[push_supabase] Inserted booking: id={booking_id}")

    return {"status": "inserted", "booking_id": booking_id}


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Insert a trip booking into Supabase."
    )
    parser.add_argument(
        "--trip-json",
        required=True,
        help="JSON string of extracted trip fields (from extract_trip.py)"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check for duplicate — do not insert"
    )
    args = parser.parse_args()

    try:
        trip_data = json.loads(args.trip_json)
        result = push_supabase(trip_data, check_only=args.check_only)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2))
        raise SystemExit(1)
