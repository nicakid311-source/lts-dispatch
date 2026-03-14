# WhatsApp Reminders SOP

## Goal
Send automated WhatsApp reminders to all operators before each scheduled trip,
bypassing the Twilio 24-hour session window via an approved Content Template.

## Trigger
APScheduler in `listen_whatsapp.py` calls `remind_trips.check_and_send_reminders()`
every 30 minutes automatically.

## Reminder Windows
| Window | Condition | Airtable Guard |
|--------|-----------|----------------|
| 24h | `3 < hours_until <= 24` | `Reminder 24h Sent` not checked |
| 3h  | `2 < hours_until <= 3`  | `Reminder 3h Sent` not checked  |
| 2h  | `0 < hours_until <= 2`  | `Reminder 2h Sent` not checked  |

## Script
`execution/remind_trips.py`

## Approved Template: lts_trip_reminder_v3
- SID stored in `.env` as `TWILIO_REMINDER_TEMPLATE_SID`
- Approved by Meta — bypasses the Twilio 24-hour session window
- Template variable mapping:

| Variable | Field |
|----------|-------|
| `{{1}}` | Reminder label (e.g. "24 hours") |
| `{{2}}` | Pickup datetime (e.g. "Fri Mar 13 - 9:00 AM") |
| `{{3}}` | Pickup location |
| `{{4}}` | Dropoff location |
| `{{5}}` | Passenger name |
| `{{6}}` | Vehicle type + affiliate (e.g. "SUV - via Lyft") |

## Fallback
If `TWILIO_REMINDER_TEMPLATE_SID` is empty or unset, the system falls back to a
free-form message via `send_whatsapp()`. Free-form messages are subject to the
Twilio 24-hour session window and may not deliver outside active conversations.

## Operators
All numbers in `OPERATOR_WHATSAPP_NUMBER` (comma-separated in `.env`).
Every reminder is sent to every operator. The Airtable checkbox is only marked
`True` after all operators have been notified successfully.

## Error Handling
- If any send fails → Airtable checkbox is NOT updated → next 30-min run retries
- All errors are logged with sensitive data sanitized (`[REDACTED]`)
- Never silently skip a failed send

## Edge Cases
- Cancelled trips are excluded via Airtable formula filter (`{Status} != 'cancelled'`)
- Trips with unparseable `Pickup Date/Time` are skipped silently
- Duplicate sends prevented by Airtable checkbox guards
- If template is rejected by Twilio: clear `TWILIO_REMINDER_TEMPLATE_SID` in `.env` to fall back to free-form

## Learnings
- `lts_trip_reminder_v3` approved by Meta on 2026-03-13. SID stored in `.env` starts with `HX92d`. The original pending SID (`HXac3...`) was replaced upon approval — always use the SID issued at approval time, not the one from submission.
