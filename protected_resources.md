# Protected Resources — LTS WhatsApp Pipeline

## Database Tables (Supabase)
- bookings → PROTECTED. Confirm before any DELETE or bulk UPDATE.
- users → PROTECTED. Read-only in this pipeline.
- drivers → PROTECTED. Read-only in this pipeline.

## External Services
- Twilio → approved for inbound webhook + outbound WhatsApp messages.
  Never send bulk messages without operator confirmation.
- Airtable → approved for single record inserts only.
  No bulk deletes or table restructuring without user approval.
- Anthropic Claude API → approved for trip extraction only.
  Each call costs tokens — do not call in loops without rate limiting.

## Read-Only Files
- .env → never modify, never log contents

## PII Handling
Passenger names, phone numbers, and pickup addresses are PII.
Never write these to .tmp/ or any log file.
Sanitize all error output before logging.
```

---

**How Claude Code Orchestrates It:**

When a webhook fires or you manually pass a thread in, Claude Code reads the directives and calls scripts in this order:
```
1. execution/extract_trip.py        → is this a confirmed job?
2. if yes → execution/push_supabase.py
3. simultaneously → execution/push_airtable.py
4. execution/send_whatsapp.py       → confirm back to operator
5. (morning cron) daily_checklist.py → post today's trip list
6. (on DONE/CANCEL reply) → push_supabase.py + push_airtable.py update