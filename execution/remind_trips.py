"""
remind_trips.py
---------------
Checks Airtable for upcoming trips and sends WhatsApp reminders to ALL operators.

Reminders:
  - 24 hours before pickup (if "Reminder 24h Sent" not already checked)
  -  2 hours before pickup (if "Reminder 2h Sent"  not already checked)
  -  3 hours before pickup (if "Reminder 3h Sent"  not already checked)

Called automatically by APScheduler in listen_whatsapp.py every 30 minutes.

Rules:
- Never log PII from trip fields
- Never send duplicate reminders (Airtable checkbox guards this)
- send_whatsapp.py handles the dev mode guard automatically
"""

import os
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from pyairtable import Api

from send_whatsapp import send_whatsapp, send_whatsapp_template

load_dotenv()


# ── Sanitize error messages before any logging ────────────────────────────────

def sanitize(msg: str) -> str:
    return re.sub(
        r'(key|token|sid|secret|password|authorization|https?://[^\s]+)',
        '[REDACTED]', str(msg), flags=re.IGNORECASE
    )


# ── Airtable client ───────────────────────────────────────────────────────────

def get_table():
    api_key    = os.getenv("AIRTABLE_API_KEY")
    base_id    = os.getenv("AIRTABLE_BASE_ID")
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "Bookings")
    if not api_key or not base_id:
        raise EnvironmentError("AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env")
    return Api(api_key).table(base_id, table_name)


# ── Fetch trips that need reminders ──────────────────────────────────────────

def fetch_reminder_candidates(table) -> list:
    """
    Fetch all non-cancelled trips that have a pickup datetime set.
    Includes reminder sent flags so we can skip already-notified trips.
    """
    formula = (
        "AND("
        "{Status} != 'cancelled', "
        "{Pickup Date/Time} != ''"
        ")"
    )
    try:
        return table.all(
            formula=formula,
            sort=["Pickup Date/Time"],
            fields=[
                "Passenger Name", "Pickup Date/Time", "Pickup Location",
                "Dropoff Location", "Vehicle Type", "Affiliate",
                "Reminder 24h Sent", "Reminder 2h Sent", "Reminder 3h Sent",
                "Submitted By"
            ]
        )
    except Exception as e:
        raise RuntimeError(f"Airtable query failed: {sanitize(str(e))}") from None


# ── Build reminder message ────────────────────────────────────────────────────

def build_reminder_msg(record: dict, label: str) -> str:
    fields    = record.get("fields", {})
    raw_dt    = fields.get("Pickup Date/Time", "")
    pickup    = fields.get("Pickup Location")  or "TBD"
    dropoff   = fields.get("Dropoff Location") or "TBD"
    name      = fields.get("Passenger Name")   or "Unknown"
    vehicle   = fields.get("Vehicle Type")     or ""
    affiliate = fields.get("Affiliate")        or ""

    try:
        local_tz = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "America/New_York"))
        dt_obj   = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=local_tz)
        dt_local = dt_obj.astimezone(local_tz)
        date_str = dt_local.strftime("%a %b %d").lstrip("0").replace(" 0", " ")
        time_str = dt_local.strftime("%I:%M %p").lstrip("0")
        dt_label = f"{date_str} - {time_str}"
    except (ValueError, AttributeError):
        dt_label = raw_dt or "TBD"

    sub_parts = []
    if vehicle:
        sub_parts.append(vehicle)
    if affiliate:
        sub_parts.append(f"via {affiliate}")
    sub_line = f"\n   {' - '.join(sub_parts)}" if sub_parts else ""

    return (
        f"LTS - Trip Reminder ({label})\n\n"
        f"[ ] {dt_label} | {pickup} -> {dropoff} | {name}"
        f"{sub_line}"
    )


# ── Template variable builder ─────────────────────────────────────────────────

def build_reminder_vars(record: dict, label: str) -> dict:
    """
    Build the template variable dict for a reminder Content Template.
    Mirrors the field extraction in build_reminder_msg().

    Expected template body:
        LTS - Trip Reminder ({{1}})

        [ ] {{2}} | {{3}} -> {{4}} | {{5}}
        {{6}}
    """
    fields    = record.get("fields", {})
    raw_dt    = fields.get("Pickup Date/Time", "")
    pickup    = fields.get("Pickup Location")  or "TBD"
    dropoff   = fields.get("Dropoff Location") or "TBD"
    name      = fields.get("Passenger Name")   or "Unknown"
    vehicle   = fields.get("Vehicle Type")     or ""
    affiliate = fields.get("Affiliate")        or ""

    try:
        local_tz = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "America/New_York"))
        dt_obj   = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=local_tz)
        dt_local = dt_obj.astimezone(local_tz)
        date_str = dt_local.strftime("%a %b %d").lstrip("0").replace(" 0", " ")
        time_str = dt_local.strftime("%I:%M %p").lstrip("0")
        dt_label = f"{date_str} - {time_str}"
    except (ValueError, AttributeError):
        dt_label = raw_dt or "TBD"

    sub_parts = []
    if vehicle:
        sub_parts.append(vehicle)
    if affiliate:
        sub_parts.append(f"via {affiliate}")
    sub_line = " - ".join(sub_parts)

    return {
        "1": label,
        "2": dt_label,
        "3": pickup,
        "4": dropoff,
        "5": name,
        "6": sub_line,
    }


def _send_reminder(op: str, record: dict, label: str):
    """
    Send a reminder to a single operator.
    Uses a Content Template if TWILIO_REMINDER_TEMPLATE_SID is set (bypasses
    the 24-hour session window). Falls back to a free-form message otherwise.
    """
    template_sid = os.getenv("TWILIO_REMINDER_TEMPLATE_SID")
    if template_sid:
        variables = build_reminder_vars(record, label)
        send_whatsapp_template(to=op, content_sid=template_sid, variables=variables)
    else:
        msg = build_reminder_msg(record, label)
        send_whatsapp(to=op, message=msg)


# ── Main reminder check ───────────────────────────────────────────────────────

def check_and_send_reminders():
    """
    Check all upcoming trips and send WhatsApp reminders as needed.
    Called every 30 minutes by APScheduler in listen_whatsapp.py.
    """
    all_operators = [n.strip() for n in os.getenv("OPERATOR_WHATSAPP_NUMBER", "").split(",") if n.strip()]
    if not all_operators:
        print("[remind_trips] No operator numbers configured — skipping")
        return

    try:
        table   = get_table()
        records = fetch_reminder_candidates(table)
    except Exception as e:
        print(f"[remind_trips] Could not fetch trips: {sanitize(str(e))}")
        return

    now = datetime.now(timezone.utc)

    for record in records:
        fields    = record.get("fields", {})
        record_id = record["id"]
        raw_dt    = fields.get("Pickup Date/Time", "")

        try:
            pickup_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            if pickup_dt.tzinfo is None:
                local_tz  = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "America/New_York"))
                pickup_dt = pickup_dt.replace(tzinfo=local_tz)
        except (ValueError, AttributeError):
            continue  # skip trips with unparseable datetime

        hours_until = (pickup_dt - now).total_seconds() / 3600

        # ── 24h reminder ─────────────────────────────────────────────────────
        if 3 < hours_until <= 24 and not fields.get("Reminder 24h Sent"):
            try:
                for op in all_operators:
                    _send_reminder(op, record, "24 hours")
                table.update(record_id, {"Reminder 24h Sent": True})
                print(f"[remind_trips] 24h reminder sent to {len(all_operators)} operators (rec={record_id})")
            except Exception as e:
                print(f"[remind_trips] Failed to send 24h reminder: {sanitize(str(e))}")

        # ── 2h reminder ──────────────────────────────────────────────────────
        if 0 < hours_until <= 2 and not fields.get("Reminder 2h Sent"):
            try:
                for op in all_operators:
                    _send_reminder(op, record, "2 hours")
                table.update(record_id, {"Reminder 2h Sent": True})
                print(f"[remind_trips] 2h reminder sent to {len(all_operators)} operators (rec={record_id})")
            except Exception as e:
                print(f"[remind_trips] Failed to send 2h reminder: {sanitize(str(e))}")

        # ── 3h reminder ──────────────────────────────────────────────────────
        if 2 < hours_until <= 3 and not fields.get("Reminder 3h Sent"):
            try:
                for op in all_operators:
                    _send_reminder(op, record, "3 hours")
                table.update(record_id, {"Reminder 3h Sent": True})
                print(f"[remind_trips] 3h reminder sent to {len(all_operators)} operators (rec={record_id})")
            except Exception as e:
                print(f"[remind_trips] Failed to send 3h reminder: {sanitize(str(e))}")
