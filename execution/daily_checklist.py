"""
daily_checklist.py
------------------
Morning cron script: queries today's trips from Airtable and sends a
formatted WhatsApp checklist to the operator via send_whatsapp.py.

Data store: Airtable only — no Supabase.

Trigger: scheduled cron job (default 5AM, configured externally)
Run:     python daily_checklist.py
         python daily_checklist.py --date 2025-03-10

Checklist format (per checklist_manager directive):
  📋 LTS — [Weekday Month Day]

  ☐ 1. [HH:MM AM/PM] | [pickup] → [dropoff] | [passenger]
  ☐ 2. ...

  Reply: DONE 1 · CANCEL 2 · UPDATE 1 [note] · STATUS

Rules:
- Read-only Airtable query (no writes from this script)
- Never log PII (names, addresses)
- send_whatsapp.py handles the dev mode guard automatically
"""

import os
import re
import json
import argparse
from datetime import datetime, date
from dotenv import load_dotenv
from pyairtable import Api

from send_whatsapp import send_whatsapp

load_dotenv()


# ── Sanitize error messages before any logging ───────────────────────────────

def sanitize(msg: str) -> str:
    return re.sub(
        r'(key|token|sid|secret|password|authorization)[\s=:]+\S+',
        '[REDACTED]', str(msg), flags=re.IGNORECASE
    )


# ── Airtable client setup ─────────────────────────────────────────────────────

def get_table():
    api_key    = os.getenv("AIRTABLE_API_KEY")
    base_id    = os.getenv("AIRTABLE_BASE_ID")
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "Bookings")
    if not api_key or not base_id:
        raise EnvironmentError("AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env")
    return Api(api_key).table(base_id, table_name)


# ── Query today's trips from Airtable ────────────────────────────────────────

def fetch_todays_trips(table, for_date: date) -> list:
    """
    Query Airtable for all non-cancelled trips on the given date.
    Uses Airtable formula to filter by date and exclude cancelled records.
    Returns list of Airtable record dicts, ordered by Pickup Date/Time.
    """
    date_str = for_date.strftime("%Y-%m-%d")

    # Airtable formula: match day of Pickup Date/Time and exclude cancelled
    formula = (
        f"AND("
        f"DATESTR({{Pickup Date/Time}}) = '{date_str}', "
        f"{{Status}} != 'cancelled'"
        f")"
    )

    try:
        records = table.all(
            formula=formula,
            sort=["Pickup Date/Time"],
            fields=[
                "Passenger Name",
                "Pickup Date/Time",
                "Pickup Location",
                "Dropoff Location",
                "Status",
            ]
        )
    except Exception as e:
        raise RuntimeError(f"Airtable query failed: {sanitize(str(e))}") from None

    return records


# ── Format the checklist message ─────────────────────────────────────────────

def format_checklist(records: list, for_date: date) -> str:
    """
    Build the WhatsApp checklist message from Airtable records.
    Passenger name is included — operator needs it for identification.
    """
    # Date header: e.g. "Thursday March 5"
    day_str = for_date.strftime("%A %B %-d") if os.name != "nt" else for_date.strftime("%A %B %d").lstrip("0").replace(" 0", " ")

    if not records:
        return "No trips scheduled for today."

    lines = [f"📋 LTS — {day_str}", ""]

    for i, record in enumerate(records, start=1):
        fields  = record.get("fields", {})

        # Parse pickup time from Airtable dateTime string (ISO format)
        raw_dt  = fields.get("Pickup Date/Time", "")
        try:
            dt       = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            time_str = dt.strftime("%I:%M %p").lstrip("0")
        except (ValueError, AttributeError):
            time_str = raw_dt or "TBD"

        pickup  = fields.get("Pickup Location")  or "TBD"
        dropoff = fields.get("Dropoff Location") or "TBD"
        name    = fields.get("Passenger Name")   or "Unknown"

        lines.append(f"☐ {i}. {time_str} | {pickup} → {dropoff} | {name}")

    lines.append("")
    lines.append("Reply: DONE 1 · CANCEL 2 · UPDATE 1 [note] · STATUS")

    return "\n".join(lines)


# ── Main function ─────────────────────────────────────────────────────────────

def send_checklist(for_date: date = None) -> dict:
    """
    Fetch today's trips from Airtable and send the checklist to the operator.
    """
    if for_date is None:
        for_date = date.today()

    operator_number = os.getenv("OPERATOR_WHATSAPP_NUMBER")
    if not operator_number:
        raise EnvironmentError("OPERATOR_WHATSAPP_NUMBER must be set in .env")

    table   = get_table()
    records = fetch_todays_trips(table, for_date)
    message = format_checklist(records, for_date)

    # send_whatsapp handles the dev mode guard internally
    result  = send_whatsapp(to=operator_number, message=message)

    # Log count only — no PII
    print(f"[daily_checklist] Sent: date={for_date.isoformat()} trips={len(records)}")

    return {
        "status":      result["status"],
        "date":        for_date.isoformat(),
        "trip_count":  len(records),
    }


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send today's trip checklist to the operator via WhatsApp."
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date to query in YYYY-MM-DD format. Defaults to today."
    )
    args = parser.parse_args()

    target_date = None
    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            print(json.dumps({"error": f"Invalid date: {args.date}. Use YYYY-MM-DD."}, indent=2))
            raise SystemExit(1)

    try:
        result = send_checklist(for_date=target_date)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2))
        raise SystemExit(1)
