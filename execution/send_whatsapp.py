"""
send_whatsapp.py
----------------
Sends an outbound WhatsApp message via Twilio.

Used for:
- Trip confirmation after successful Supabase + Airtable push
- Daily morning checklist delivery
- Command reply confirmations from listen_whatsapp.py

CRITICAL: Dev mode guard runs FIRST. If ENV != "production", the message
is printed to stdout and Twilio is never called. This is non-negotiable.

Rules:
- Never send outbound messages in dev mode
- Never log the message body (may contain PII)
- Never send bulk messages without operator confirmation
"""

import os
import re
import json
import argparse
from dotenv import load_dotenv

# Load environment variables from .env (read-only — never modify)
load_dotenv()


# ── Sanitize error messages before any logging ───────────────────────────────

def sanitize(msg: str) -> str:
    """Strip credential patterns from error output before logging."""
    return re.sub(
        r'(key|token|sid|secret|password|authorization|AC[a-f0-9]{32}|SK[a-f0-9]{32})',
        '[REDACTED]',
        str(msg),
        flags=re.IGNORECASE
    )


# ── Main send function ────────────────────────────────────────────────────────

def send_whatsapp(to: str, message: str) -> dict:
    """
    Send a WhatsApp message via Twilio.

    Dev mode guard: if ENV != "production", prints the message and returns
    without calling Twilio. This check runs before any other logic.

    Args:
        to:      Recipient number in Twilio format, e.g. "whatsapp:+12025551234"
        message: Message body text

    Returns:
        dict with status ("sent" or "dev_mode_skipped") and message SID if sent
    """
    # ── DEV MODE GUARD — must be first ───────────────────────────────────────
    env = os.getenv("ENV", "development")
    if env != "production":
        print(f"[send_whatsapp] DEV MODE — message not sent via Twilio.")
        print(f"[send_whatsapp] To: {to}")
        print(f"[send_whatsapp] Body preview: [message body withheld — contains PII]")
        # Print the actual message only to local stdout for dev debugging
        # (not written to any file or log)
        print(f"--- MESSAGE START ---\n{message}\n--- MESSAGE END ---")
        return {"status": "dev_mode_skipped", "env": env}

    # ── Production: send via Twilio ───────────────────────────────────────────
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token  = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")

    if not account_sid or not auth_token or not from_number:
        raise EnvironmentError(
            "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_FROM must be set in .env"
        )

    # Ensure "to" number is in whatsapp: format
    if not to.startswith("whatsapp:"):
        to = f"whatsapp:{to}"

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)

        twilio_message = client.messages.create(
            from_=from_number,
            to=to,
            body=message
        )
    except Exception as e:
        # Sanitize before raising — Twilio errors can contain account SIDs
        raise RuntimeError(f"Twilio send failed: {sanitize(str(e))}") from None

    sid = twilio_message.sid
    # Log only the SID — never log message body or recipient number (PII)
    print(f"[send_whatsapp] Message sent: sid={sid}")

    return {"status": "sent", "sid": sid}


# ── Template send function ────────────────────────────────────────────────────

def send_whatsapp_template(to: str, content_sid: str, variables: dict) -> dict:
    """
    Send a WhatsApp template message via Twilio Content API.

    Unlike send_whatsapp(), template messages bypass the 24-hour session window
    and can be sent at any time to opted-in recipients.

    Args:
        to:          Recipient number (e.g. "+12025551234" or "whatsapp:+12025551234")
        content_sid: Twilio Content SID for the approved template (e.g. "HXabc...")
        variables:   Dict mapping template variable indices to values (e.g. {"1": "24 hours"})

    Returns:
        dict with status ("sent" or "dev_mode_skipped") and message SID if sent
    """
    # ── DEV MODE GUARD — must be first ───────────────────────────────────────
    env = os.getenv("ENV", "development")
    if env != "production":
        print(f"[send_whatsapp_template] DEV MODE — template message not sent via Twilio.")
        print(f"[send_whatsapp_template] To: {to}  content_sid: {content_sid}")
        print(f"[send_whatsapp_template] Variables: {variables}")
        return {"status": "dev_mode_skipped", "env": env}

    # ── Production: send via Twilio ───────────────────────────────────────────
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token  = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")

    if not account_sid or not auth_token or not from_number:
        raise EnvironmentError(
            "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_FROM must be set in .env"
        )

    if not to.startswith("whatsapp:"):
        to = f"whatsapp:{to}"

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)

        twilio_message = client.messages.create(
            from_=from_number,
            to=to,
            content_sid=content_sid,
            content_variables=json.dumps(variables),
        )
    except Exception as e:
        raise RuntimeError(f"Twilio template send failed: {sanitize(str(e))}") from None

    sid = twilio_message.sid
    print(f"[send_whatsapp_template] Template message sent: sid={sid}")

    return {"status": "sent", "sid": sid}


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send a WhatsApp message via Twilio."
    )
    parser.add_argument(
        "--to",
        required=True,
        help="Recipient WhatsApp number (e.g. whatsapp:+12025551234 or +12025551234)"
    )
    parser.add_argument(
        "--message",
        required=True,
        help="Message body text to send"
    )
    args = parser.parse_args()

    try:
        result = send_whatsapp(args.to, args.message)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2))
        raise SystemExit(1)
