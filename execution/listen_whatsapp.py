"""
listen_whatsapp.py
------------------
Twilio webhook receiver (Flask). Receives inbound WhatsApp messages and
routes them to the correct handler.

Data store: Airtable only — no Supabase.

Start:  python listen_whatsapp.py
Port:   5000 (default) — expose via ngrok for local dev
Route:  POST /webhook  — set this as your Twilio WhatsApp webhook URL

Routing logic:
  1. Message from operator + matches command pattern → handle command
  2. Message from operator + no command pattern     → treat as new trip thread
  3. Message from unknown sender                    → log and ignore

Commands (from checklist_manager directive):
  DONE [n]          → mark trip n complete in Airtable
  CANCEL [n]        → mark trip n cancelled in Airtable
  UPDATE [n] [note] → append note to trip n in Airtable
  STATUS            → reply with pending/done count for today

Rules:
- Never log PII from message body
- Never silently skip a failed update — notify operator
- DONE on already-completed trip → ignore silently
- Twilio requires TwiML XML response for all replies
"""

import os
import re
import json
import base64
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from flask import Flask, request, Response
from pyairtable import Api
import requests as http_requests
from requests.auth import HTTPBasicAuth

from apscheduler.schedulers.background import BackgroundScheduler
from extract_trip import extract_trip, extract_trip_from_image
from send_whatsapp import send_whatsapp
from remind_trips import check_and_send_reminders

load_dotenv()

app = Flask(__name__)

# ── Trip reminder scheduler (runs every 30 minutes) ───────────────────────────
_scheduler = BackgroundScheduler()
_scheduler.add_job(check_and_send_reminders, 'interval', minutes=30,
                   next_run_time=datetime.now(timezone.utc))
_scheduler.start()


# ── Sanitize error messages before any logging ───────────────────────────────

def sanitize(msg: str) -> str:
    return re.sub(
        r'(key|token|sid|secret|password|authorization|https?://[^\s]+)',
        '[REDACTED]', str(msg), flags=re.IGNORECASE
    )


# ── TwiML response builder ────────────────────────────────────────────────────

def twiml_reply(body: str) -> Response:
    """Return a valid TwiML XML response that Twilio expects."""
    safe_body = (body
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;"))
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe_body}</Message></Response>'
    return Response(xml, mimetype="text/xml")


# ── Airtable client setup ─────────────────────────────────────────────────────

def get_table():
    """Initialize and return the Airtable Bookings table."""
    api_key    = os.getenv("AIRTABLE_API_KEY")
    base_id    = os.getenv("AIRTABLE_BASE_ID")
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "Bookings")
    if not api_key or not base_id:
        raise EnvironmentError("AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env")
    return Api(api_key).table(base_id, table_name)


# ── Fetch today's ordered trip list from Airtable ────────────────────────────

def fetch_todays_trips(table) -> list:
    """
    Query Airtable for today's non-cancelled bookings, ordered by pickup time.
    Used to resolve trip numbers (DONE 1, CANCEL 2, etc.)
    Each returned record has an 'id' field = Airtable record ID (recXXXXXX).
    """
    today   = date.today().strftime("%Y-%m-%d")
    formula = (
        f"AND("
        f"DATESTR({{Pickup Date/Time}}) = '{today}', "
        f"{{Status}} != 'cancelled'"
        f")"
    )

    try:
        records = table.all(
            formula=formula,
            sort=["Pickup Date/Time"],
            fields=["Passenger Name", "Pickup Date/Time", "Pickup Location",
                    "Dropoff Location", "Status", "Notes"]
        )
    except Exception as e:
        raise RuntimeError(f"Airtable query failed: {sanitize(str(e))}") from None

    return records


# ── Fetch all trips (across all dates) from Airtable ─────────────────────────

def fetch_all_trips(table) -> list:
    """All non-cancelled bookings ordered by pickup time (across all dates)."""
    formula = "{Status} != 'cancelled'"
    try:
        return table.all(
            formula=formula,
            sort=["Pickup Date/Time"],
            fields=["Passenger Name", "Pickup Date/Time", "Pickup Location",
                    "Dropoff Location", "Status", "Notes", "Vehicle Type", "Affiliate", "Price"]
        )
    except Exception as e:
        raise RuntimeError(f"Airtable query failed: {sanitize(str(e))}") from None


# ── Update a trip record in Airtable ─────────────────────────────────────────

def update_airtable_record(table, record_id: str, new_status: str, note: str = None) -> None:
    """
    Update a booking's Status (and optionally append to Notes) in Airtable
    using the Airtable record ID (recXXXXXX).
    """
    fields = {"Status": new_status}

    if note:
        # Fetch existing notes first to append rather than overwrite
        try:
            existing = table.get(record_id)
            existing_notes = existing.get("fields", {}).get("Notes", "") or ""
            fields["Notes"] = f"{existing_notes}\n{note}".strip() if existing_notes else note
        except Exception:
            fields["Notes"] = note  # fallback: just set the note

    try:
        table.update(record_id, fields)
    except Exception as e:
        raise RuntimeError(f"Airtable update failed: {sanitize(str(e))}") from None


# ── Command: STATUS ───────────────────────────────────────────────────────────

def handle_status(table) -> str:
    """Return count of pending, completed, and cancelled trips for today."""
    today   = date.today().strftime("%Y-%m-%d")
    formula = f"DATESTR({{Pickup Date/Time}}) = '{today}'"

    try:
        all_records = table.all(formula=formula, fields=["Status"])
    except Exception as e:
        return f"Could not fetch status: {sanitize(str(e))}"

    statuses  = [r.get("fields", {}).get("Status", "") for r in all_records]
    pending   = statuses.count("pending")
    completed = statuses.count("completed")
    cancelled = statuses.count("cancelled")

    return f"Today: {pending} pending · {completed} done · {cancelled} cancelled"


# ── Build full trip list reply ────────────────────────────────────────────────

MAX_LIST_CHARS = 1400  # stay safely under Twilio's 1600-char WhatsApp limit

def build_trip_list_reply(table) -> str:
    """Build a full trip list reply across all dates in checklist format."""
    try:
        records = fetch_all_trips(table)
    except Exception as e:
        return f"Could not fetch trips: {sanitize(str(e))}"

    if not records:
        return "LTS — All Trips\n\nNo trips on record."

    lines = ["LTS — All Trips", ""]
    shown = 0
    for i, record in enumerate(records, start=1):
        fields   = record.get("fields", {})
        raw_dt   = fields.get("Pickup Date/Time", "")
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
        pickup    = fields.get("Pickup Location")  or "TBD"
        dropoff   = fields.get("Dropoff Location") or "TBD"
        name      = fields.get("Passenger Name")   or "Unknown"
        vehicle   = fields.get("Vehicle Type")     or ""
        affiliate = fields.get("Affiliate")        or ""
        price     = fields.get("Price")            or ""
        notes     = fields.get("Notes")            or ""

        trip_lines = [f"[ ] {i}. {dt_label} | {pickup} -> {dropoff} | {name}"]
        sub_parts = []
        if vehicle:
            sub_parts.append(vehicle)
        if affiliate:
            sub_parts.append(f"via {affiliate}")
        if price:
            # Airtable returns currency as float (e.g. 150.0) — format as $150
            try:
                price_str = f"${int(float(price))}" if float(price) == int(float(price)) else f"${float(price)}"
            except (ValueError, TypeError):
                price_str = f"${price}" if not str(price).startswith("$") else str(price)
            sub_parts.append(price_str)
        if sub_parts:
            trip_lines.append(f"   {' - '.join(sub_parts)}")
        if notes:
            trip_lines.append(f"   📝 {notes}")

        candidate = "\n".join(lines + trip_lines)
        if len(candidate) > MAX_LIST_CHARS:
            remaining = len(records) - shown
            lines.append(f"...+{remaining} more trips not shown")
            break

        lines.extend(trip_lines)
        shown += 1

    done_footer = " - ".join(f"DONE {i}" for i in range(1, shown + 1))
    lines.append("")
    lines.append(f"Reply: {done_footer}")
    return "\n".join(lines)


# ── Command handler ───────────────────────────────────────────────────────────

def handle_command(body: str, table) -> str:
    """
    Parse and execute an operator command. Returns the reply string.
    Trip numbers are 1-indexed positions in today's ordered trip list.
    """
    body_upper = body.strip().upper()

    # STATUS
    if body_upper == "STATUS":
        return handle_status(table)

    # LIST
    if body_upper == "LIST":
        return build_trip_list_reply(table)

    # DONE / CANCEL: e.g. "DONE 2" or "CANCEL 3"
    done_match   = re.match(r'^(DONE|CANCEL)\s+(\d+)$', body_upper)
    # UPDATE: e.g. "UPDATE 2 flight delayed 30min"
    update_match = re.match(r'^UPDATE\s+(\d+)\s+(.+)$', body.strip(), re.IGNORECASE)

    if done_match:
        action = done_match.group(1)
        n      = int(done_match.group(2))

        trips = fetch_all_trips(table)

        if n < 1 or n > len(trips):
            return f"Trip {n} not found. ({len(trips)} trips pending)"

        record    = trips[n - 1]
        record_id = record["id"]

        if action == "DONE":
            # Delete the record — completed trips are not kept in Airtable
            try:
                table.delete(record_id)
            except Exception as e:
                return f"Failed to delete trip {n}: {sanitize(str(e))}"
            print(f"[listen_whatsapp] Trip {n} (rec={record_id}) deleted (done)")
        else:
            # CANCEL — mark cancelled so it drops from fetch_all_trips filter
            try:
                update_airtable_record(table, record_id, "cancelled")
            except Exception as e:
                return f"Failed to cancel trip {n}: {sanitize(str(e))}"
            print(f"[listen_whatsapp] Trip {n} (rec={record_id}) cancelled")

        return build_trip_list_reply(table)

    elif update_match:
        n    = int(update_match.group(1))
        note = update_match.group(2).strip()

        trips = fetch_all_trips(table)

        if n < 1 or n > len(trips):
            return f"Trip {n} not found for today. ({len(trips)} trips scheduled)"

        record    = trips[n - 1]
        record_id = record["id"]
        current_status = record.get("fields", {}).get("Status", "pending")

        try:
            update_airtable_record(table, record_id, current_status, note=note)
        except Exception as e:
            return f"Failed to add note to trip {n}: {sanitize(str(e))}"

        print(f"[listen_whatsapp] Note added to trip {n} (rec={record_id})")
        return f"Note added to trip {n}."

    else:
        return (
            "Commands:\n"
            "  DONE [n]            → mark trip complete\n"
            "  CANCEL [n]          → mark trip cancelled\n"
            "  UPDATE [n] [note]   → add a note to trip\n"
            "  STATUS              → today's trip count"
        )


# ── Command pattern detection ─────────────────────────────────────────────────

COMMAND_PATTERN = re.compile(
    r'^(DONE\s+\d+|CANCEL\s+\d+|UPDATE\s+\d+\s+.+|STATUS|LIST)$',
    re.IGNORECASE
)

def is_command(body: str) -> bool:
    return bool(COMMAND_PATTERN.match(body.strip()))


# ── Webhook route ─────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Main Twilio webhook endpoint. Receives all inbound WhatsApp messages.

    Twilio POST body fields used:
        Body  — message text
        From  — sender's WhatsApp number (e.g. whatsapp:+12025551234)
    """
    body     = request.form.get("Body", "").strip()
    from_num = request.form.get("From", "").strip()

    _raw_numbers = os.getenv("OPERATOR_WHATSAPP_NUMBER", "")
    _operator_numbers = [n.strip() for n in _raw_numbers.split(",") if n.strip()]

    def normalize(num):
        return num.replace("whatsapp:", "").strip()

    is_operator = normalize(from_num) in [normalize(n) for n in _operator_numbers]

    # Log sender status only — never log message body (PII risk)
    print(f"[listen_whatsapp] Inbound: from={'operator' if is_operator else 'unknown'}")

    # ── Route: operator command ───────────────────────────────────────────────
    if is_operator and is_command(body):
        try:
            table = get_table()
            reply = handle_command(body, table)
        except Exception as e:
            reply = f"Error processing command: {sanitize(str(e))}"
        return twiml_reply(reply)

    # ── Route: new trip thread from operator ─────────────────────────────────
    if is_operator:
        operator_phone = normalize(from_num)
        num_media = int(request.form.get("NumMedia", 0))

        try:
            if num_media > 0:
                media_url   = request.form.get("MediaUrl0", "")
                media_type  = request.form.get("MediaContentType0", "image/jpeg")
                account_sid = os.getenv("TWILIO_ACCOUNT_SID")
                auth_token  = os.getenv("TWILIO_AUTH_TOKEN")
                img_resp = http_requests.get(
                    media_url,
                    auth=HTTPBasicAuth(account_sid, auth_token),
                    timeout=10
                )
                img_resp.raise_for_status()
                image_b64 = base64.b64encode(img_resp.content).decode("utf-8")
                trips = extract_trip_from_image(image_b64, media_type, operator_phone)
                # Operator sending a screenshot is implicit acceptance + high confidence
                for t in trips:
                    if t.get("is_trip_request"):
                        t["accepted"] = True
                        t["confidence"] = "high"
            else:
                trips = extract_trip(thread=body, operator_phone=operator_phone)
        except Exception as e:
            return twiml_reply(f"Trip extraction failed: {sanitize(str(e))}")

        trip_requests = [t for t in trips if t.get("is_trip_request")]
        if not trip_requests:
            return twiml_reply("Message received — not identified as a trip request.")

        from push_airtable import push_airtable
        saved = []
        low_confidence_count = 0

        for trip in trip_requests:
            confidence = trip.get("confidence", "low")
            if confidence in ("high", "medium"):
                try:
                    trip["submitted_by"] = normalize(from_num)
                    result = push_airtable(trip)
                    airtable_id = result.get("airtable_id", "")
                    print(f"[listen_whatsapp] Auto-pushed trip to Airtable: rec={airtable_id}")
                    saved.append(trip)
                except Exception as e:
                    return twiml_reply(f"Trip detected but Airtable push failed: {sanitize(str(e))}")
            else:
                low_confidence_count += 1

        if not saved:
            return twiml_reply(
                "Trip detected (confidence: low). Flagged for manual review — not saved."
            )

        # Build confirmation message
        if len(saved) == 1:
            trip     = saved[0]
            name     = trip.get("passenger_name")  or "Unknown"
            pickup   = trip.get("pickup_location")  or "TBD"
            dropoff  = trip.get("dropoff_location") or "TBD"
            vehicle  = trip.get("vehicle_type")     or ""
            affiliate = trip.get("affiliate")       or ""
            dt_raw   = trip.get("pickup_datetime")  or ""
            try:
                local_tz = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "America/New_York"))
                dt_obj   = datetime.fromisoformat(dt_raw)
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=local_tz)
                dt_local = dt_obj.astimezone(local_tz)
                day_str  = dt_local.strftime("%A %B %d").lstrip("0").replace(" 0", " ")
                time_str = dt_local.strftime("%I:%M %p").lstrip("0")
            except (ValueError, TypeError):
                day_str  = "TBD"
                time_str = "TBD"
            price     = trip.get("price") or ""
            sub_parts = []
            if vehicle:
                sub_parts.append(vehicle)
            if affiliate:
                sub_parts.append(f"via {affiliate}")
            if price:
                sub_parts.append(f"${price}" if not str(price).startswith("$") else str(price))
            sub_line   = f"\n   {' · '.join(sub_parts)}" if sub_parts else ""
            notes      = trip.get("notes") or ""
            notes_line = f"\n📝 {notes}" if notes else ""
            msg = (
                f"📋 LTS — {day_str}\n\n"
                f"☐ 1. {time_str} | {pickup} → {dropoff} | {name}"
                f"{sub_line}"
                f"{notes_line}\n\n"
                f"✅ Saved · Send LIST for pending trips"
            )
        else:
            # Multiple trips — show a compact summary list
            local_tz = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "America/New_York"))
            lines = [f"📋 LTS — {len(saved)} trips saved\n"]
            for i, trip in enumerate(saved, start=1):
                pickup  = trip.get("pickup_location")  or "TBD"
                dropoff = trip.get("dropoff_location") or "TBD"
                dt_raw  = trip.get("pickup_datetime")  or ""
                try:
                    dt_obj = datetime.fromisoformat(dt_raw)
                    if dt_obj.tzinfo is None:
                        dt_obj = dt_obj.replace(tzinfo=local_tz)
                    dt_local = dt_obj.astimezone(local_tz)
                    dt_label = dt_local.strftime("%a %b %d %I:%M %p").lstrip("0").replace(" 0", " ")
                except (ValueError, TypeError):
                    dt_label = dt_raw or "TBD"
                lines.append(f"☐ {i}. {dt_label} | {pickup} → {dropoff}")
            if low_confidence_count:
                lines.append(f"\n⚠️ {low_confidence_count} trip(s) skipped (low confidence)")
            lines.append("\nSend LIST for all pending trips")
            msg = "\n".join(lines)

        return twiml_reply(msg)

    # ── Route: unknown sender — ignore ────────────────────────────────────────
    print("[listen_whatsapp] Message from unknown sender — ignored.")
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        mimetype="text/xml"
    )


# ── App entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("ENV", "development") != "production"
    print(f"[listen_whatsapp] Starting on port {port} (debug={debug})")
    print(f"[listen_whatsapp] Set Twilio webhook URL to: http://<your-ngrok-url>/webhook")
    app.run(host="0.0.0.0", port=port, debug=debug)
