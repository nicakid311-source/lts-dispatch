# Platform Sync SOP

## Goal
Push extracted trip data to Supabase and Airtable simultaneously
after a confirmed trip is detected.

## Input
Structured JSON from extract_trip.py

## Step 1: Supabase Write
Call execution/push_supabase.py
- Table: bookings
- Fields: all extracted trip fields + source, status, created_at
- Status default: "pending"
- Never overwrite existing records — insert only
- Check for duplicate raw_message before inserting

## Step 2: Airtable Write
Call execution/push_airtable.py
- Table: Bookings
- Map fields to Airtable column names exactly
- Run after Supabase confirms success

## Step 3: Error Handling
- If Supabase fails → do not push to Airtable, flag to operator
- If Airtable fails after Supabase succeeds → log and notify operator
- Never silently skip a failed push

## Step 4: Confirmation
After both platforms confirm → call execution/send_whatsapp.py
to notify operator the booking was saved.

## Learnings
(updated as pipeline runs)
