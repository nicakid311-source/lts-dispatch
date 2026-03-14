# Trip Extraction SOP

## Goal
Detect when the operator has accepted a trip job from a WhatsApp 
thread (DM or group chat) and extract all available trip fields.

## Input
- Full message thread (original job post + operator reply)
- Operator's phone number (to identify which reply is theirs)

## Step 1: Acceptance Detection
Call extract_trip.py with the thread.
Claude determines:
- Is this a trip request? (boolean)
- Did the operator accept it? (boolean)
- Confidence level: high / medium / low

Acceptance signals include but are not limited to:
"ok", "confirmed", "we got it", "on it", "sí", "lo tenemos",
"sure", "we'll take it", any affirmative in context.

## Step 2: Field Extraction
If accepted, extract:
- passenger_name, phone, pickup_datetime, pickup_location,
  dropoff_location, vehicle_type, flight_number,
  num_passengers, price, notes, source_chat, raw_message

## Step 3: Confidence Routing
- high → auto-push to platforms
- medium → send operator a WhatsApp confirmation request first
- low → flag for manual review, do not push

## Edge Cases
- Duplicate thread: check Supabase for existing raw_message match
  before inserting — never create duplicate bookings
- Missing pickup_datetime: extract as-is, flag in notes field
- Bilingual messages: extract regardless of language
- Passenger name in pipe format: the last pipe segment may be the
  client's name (quoted or unquoted). Extract it into passenger_name
  and strip any surrounding quotes.
  Example: `Mar 21 8:15am | POM to FLL Hotel | $170 | 12 Pax | Sprinter | "Ana Lopez"`
  → passenger_name = "Ana Lopez"

## Outputs
Structured JSON passed to platform_sync directive.

## Learnings
(updated as pipeline runs in production)