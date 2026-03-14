"""
extract_trip.py
---------------
Calls Claude API to detect whether a WhatsApp thread contains confirmed
trip bookings and extracts all structured trip fields.

Input:  Full WhatsApp thread (or image) + operator phone
Output: List of structured trip dicts (one per trip found)

Rules:
- Never hallucinate missing fields — use null
- Never log thread content (PII)
- One API call per thread — no loops
- Model: claude-haiku-4-5-20251001 (cost-controlled, per protected_resources.md)
"""

import os
import re
import json
import argparse
import anthropic
from dotenv import load_dotenv

# Load environment variables from .env (read-only — never modify)
load_dotenv()


# ── Sanitize error messages before any logging ───────────────────────────────

def sanitize(msg: str) -> str:
    """Strip credential patterns from error output before logging."""
    return re.sub(
        r'(key|token|sid|secret|password|authorization)[\s=:]+\S+',
        '[REDACTED]',
        str(msg),
        flags=re.IGNORECASE
    )


# ── System prompt for the Claude extraction call ─────────────────────────────

SYSTEM_PROMPT = """You are a trip extraction engine for a transportation company's WhatsApp pipeline.

Your job: analyze a WhatsApp message thread and determine:
1. Is this a trip request? (true/false)
2. Did the operator accept the trip? (true/false)
3. Extract all available trip fields.

Acceptance signals (not exhaustive): "ok", "confirmed", "we got it", "on it",
"sure", "we'll take it", "sí", "lo tenemos", any affirmative in context.

CRITICAL RULES:
- Output ONLY valid JSON — no explanation, no markdown, no extra text
- For any field you cannot find in the thread, use null — never guess or invent values
- pickup_datetime MUST be in ISO 8601 format: "YYYY-MM-DDTHH:MM:00"
  Use the current date ONLY to resolve explicitly relative terms like "tomorrow",
  "today", "next Monday", etc.
  If no date is visible anywhere in the source, set pickup_datetime to null — do NOT
  default to today's date.
  If date is known but time is not stated, use "T00:00:00".
- Confidence scoring:
    "high"   → pickup_datetime, pickup_location, AND dropoff_location are all clearly stated
    "medium" → most fields present but date/time or one location is ambiguous or missing
    "low"    → cannot determine basic trip details (two or more key fields missing)

Output a JSON array of trip objects. Each trip must have this exact structure:
[
  {
    "is_trip_request": true,
    "accepted": true,
    "confidence": "high",
    "passenger_name": null,
    "phone": null,
    "pickup_datetime": null,
    "pickup_location": null,
    "dropoff_location": null,
    "vehicle_type": null,
    "flight_number": null,
    "num_passengers": null,
    "price": null,
    "affiliate": null,
    "notes": null,
    "source_chat": null,
    "raw_message": "<full thread text or row content>"
  }
]

- If the message contains multiple trips (e.g. a table, schedule, or list of rows), include ALL of them as separate objects in the array.
- If the input is a table or spreadsheet (e.g. columns like FECHA, HORA, DESDE, HACIA, PAX), treat each data row as a separate trip.
- If the message contains only one trip, return a single-item array.
- If the message is not a trip request at all, return: [{"is_trip_request": false}]
- CRITICAL — Merged FECHA cells: In these spreadsheets the FECHA (date) column uses Excel MERGED CELLS. Each date group spans 2–3 rows, but the date label (e.g. "Miercoles 11") appears only ONCE — always in the LAST row of the merged region. The rows ABOVE it in the same group have a visually blank FECHA cell. You MUST assign all rows in a group to the date shown at the BOTTOM of that group, not the date from the previous group. Example (do NOT reverse this logic):
    Row 2: [blank FECHA], 5:45pm  → date = Miercoles 11  (because row 3 shows "Miercoles 11")
    Row 3: Miercoles 11,  9:00pm  → date = Miercoles 11
    Row 4: [blank FECHA], 8:30am  → date = Jueves 12     (because row 5 shows "Jueves 12")
    Row 5: Jueves 12,     5:00pm  → date = Jueves 12
    Row 6: [blank FECHA], 8:30am  → date = Viernes 13    (because row 8 shows "Viernes 13")
    Row 7: [blank FECHA], 1:30pm  → date = Viernes 13
    Row 8: Viernes 13,    4:30pm  → date = Viernes 13
  NEVER assign a blank FECHA row to the date in the group ABOVE it.

Field notes:
- affiliate: the company that assigned or billed this job to us — look for the "Bill To" name (e.g. "BlackRide Inc", "Carey"). Do NOT use the "Affiliate Name" field in the itinerary (that is our own company name). Use null if not visible — never guess.
- vehicle_type: exact vehicle requested (e.g. "Sprinter Van", "Sedan", "SUV"). Use null if not stated.
- notes: capture any information from the message that doesn't fit a specific field — including passenger count, price, number of vehicles, extra context like "the same client", special instructions, or any other detail not extracted elsewhere. Do NOT leave notes null if there is unparsed information in the message.
- For any field not clearly present in the source, use null.
- Pipe-delimited shorthand: operators may send trips in this format:
    "Date Time | Pickup → Dropoff | Price | Pax | Vehicle"
    "Date | Time | Pickup → Dropoff | Price | Pax | Vehicle"
    "Date Time | Pickup → Dropoff | Price | Pax | Vehicle | Name"
  When parsing these:
    - The pickup time is always the first value that looks like a clock time (H:MMam/pm or HH:MM am/pm)
    - "12 pax" or any "N pax" is a PASSENGER COUNT — never a time
    - Pipe segments after the locations are: price (has $ or is a plain number), pax count, vehicle type
    - The LAST segment, if it is a name (quoted or unquoted, not a number or vehicle type), is the passenger_name
    - Quotes around the name ("John Smith") are optional — strip them when extracting
  Examples:
    "Mar 21 8:15am | POM → FLL Hotel | $170 | 12 pax | Sprinter"                   → time=08:15, pax=12, price=170, passenger_name=null
    "Mar 21 | 8:15am | POM → FLL Hotel | $170 | 12 pax"                            → time=08:15, pax=12, price=170, passenger_name=null
    "Mar 21 8:15am | POM to FLL Hotel | $170 | 12 Pax | Sprinter | \"John Smith\"" → time=08:15, pax=12, price=170, passenger_name="John Smith"
    "Mar 21 8:15am | POM to FLL Hotel | $170 | 12 Pax | Sprinter | Maria Garcia"   → time=08:15, pax=12, price=170, passenger_name="Maria Garcia"
- Flight itineraries: this is a PICKUP service. When you see a flight itinerary:
    - pickup_location = the ARRIVAL airport/terminal (where the limo meets the passenger on landing)
    - pickup_datetime = the ARRIVAL time and date (when the flight lands, not when it departs)
    - dropoff_location = null unless stated elsewhere in the message
    - flight_number = the airline and flight number (e.g. "Spirit Airlines 784" or "Spirit 784")
- Itineraries may be in Spanish — "Llega" = arrives, "Sale" = departs, "dom." = Sunday, "lun." = Monday, "mar." = Tuesday, "mié." = Wednesday, "jue." = Thursday, "vie." = Friday, "sáb." = Saturday.
- Always use the ARRIVAL (Llega) time and date for pickup_datetime, never the departure (Sale) time.
"""


# ── Main extraction function ──────────────────────────────────────────────────

def extract_trip(thread: str, operator_phone: str) -> list:
    """
    Send the WhatsApp thread to Claude and return a list of structured trip dicts.

    Args:
        thread:         Full WhatsApp message thread as a string
        operator_phone: Operator's phone number (used to help Claude
                        identify which reply belongs to the operator)

    Returns:
        list of dicts, one per trip found; missing fields set to null
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in .env")

    client = anthropic.Anthropic(api_key=api_key)

    # Build user prompt — include today's date so Claude can resolve relative dates
    from datetime import date
    today_str = date.today().strftime("%Y-%m-%d")
    user_prompt = (
        f"Today's date: {today_str}\n"
        f"Operator phone number: {operator_phone}\n\n"
        f"WhatsApp thread:\n{thread}"
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Cost-controlled per directive
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}]
        )
    except Exception as e:
        # Sanitize before raising — error may contain API key fragments
        raise RuntimeError(f"Claude API call failed: {sanitize(str(e))}") from None

    # Parse the JSON response from Claude
    raw_text = response.content[0].text.strip()

    # Strip markdown code fences if Claude wrapped the output (e.g. ```json ... ```)
    if raw_text.startswith("```"):
        raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
        raw_text = re.sub(r'\s*```$', '', raw_text)
        raw_text = raw_text.strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        raise ValueError(
            f"Claude returned non-JSON output. "
            f"First 100 chars: {raw_text[:100]}"
        )

    # Normalize to list (handle Claude returning a single dict defensively)
    if isinstance(result, dict):
        result = [result]

    # Ensure raw_message is populated on each trip
    for item in result:
        if not item.get("raw_message"):
            item["raw_message"] = thread

    return result


# ── Image extraction function ────────────────────────────────────────────────

def extract_trip_from_image(image_b64: str, media_type: str, operator_phone: str) -> list:
    """
    Send a base64-encoded image to Claude vision and return a list of structured trip dicts.
    Uses the same SYSTEM_PROMPT and JSON parsing logic as extract_trip().

    Args:
        image_b64:      Base64-encoded image bytes
        media_type:     MIME type (e.g. "image/jpeg", "image/png")
        operator_phone: Operator's phone number

    Returns:
        list of dicts, one per trip found; missing fields set to null
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in .env")

    client = anthropic.Anthropic(api_key=api_key)

    from datetime import date
    today_str = date.today().strftime("%Y-%m-%d")

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Today's date: {today_str}\n"
                            f"Operator phone number: {operator_phone}\n\n"
                            "Extract all trip details from this image. "
                            "If it is a table or schedule with multiple rows, extract each row as a separate trip."
                        )
                    }
                ]
            }]
        )
    except Exception as e:
        raise RuntimeError(f"Claude API call failed: {sanitize(str(e))}") from None

    raw_text = response.content[0].text.strip()

    if raw_text.startswith("```"):
        raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
        raw_text = re.sub(r'\s*```$', '', raw_text)
        raw_text = raw_text.strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        raise ValueError(
            f"Claude returned non-JSON output. "
            f"First 100 chars: {raw_text[:100]}"
        )

    # Normalize to list
    if isinstance(result, dict):
        result = [result]

    for item in result:
        if not item.get("raw_message"):
            item["raw_message"] = "[image]"

    return result


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract trip data from a WhatsApp thread using Claude."
    )
    parser.add_argument(
        "--thread",
        required=True,
        help="Full WhatsApp thread text (original post + operator reply)"
    )
    parser.add_argument(
        "--operator",
        required=True,
        help="Operator's WhatsApp phone number (e.g. +12025551234)"
    )
    args = parser.parse_args()

    try:
        results = extract_trip(args.thread, args.operator)
        # Print results — note: raw_message may contain PII, do not add extra logging
        print(json.dumps(results, indent=2, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2))
        raise SystemExit(1)
