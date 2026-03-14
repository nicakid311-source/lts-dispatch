# Checklist Manager SOP

## Goal
Give the operator a real-time trip checklist inside WhatsApp.
Every action taken in WhatsApp syncs to Supabase and Airtable.

## Morning Digest
- Trigger: scheduled cron job at operator-defined time (default 5AM)
- Script: execution/daily_checklist.py
- Query Supabase bookings table for today's trips ordered by pickup_datetime
- Format as numbered checklist and send via Twilio to operator's number

## Checklist Format
📋 LTS — [Day Date]

☐ 1. [time] | [pickup] → [dropoff] | [passenger]
☐ 2. [time] | [pickup] → [dropoff] | [passenger]

Reply: DONE 1 · CANCEL 2 · STATUS

## Command Parsing
Listen for operator replies to the checklist thread:

| Command | Action |
|---|---|
| DONE [n] | Mark trip complete in Supabase + Airtable |
| CANCEL [n] | Mark trip cancelled in Supabase + Airtable |
| UPDATE [n] [note] | Append note to trip in both platforms |
| STATUS | Reply with live count of pending/done trips today |

## Sync Rule
Every command must update BOTH platforms before confirming
back to WhatsApp. If one platform fails, notify operator —
never silently skip.

## Edge Cases
- Invalid command format → reply with usage hint
- Trip number out of range → reply "Trip [n] not found for today"
- Duplicate DONE on same trip → ignore silently

## Learnings
(updated as pipeline runs)
