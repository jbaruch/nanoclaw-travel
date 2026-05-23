---
name: check-travel-bookings
description: Checks upcoming trips for missing bookings (flights, hotels, accommodation) by reading the nightly-built `travel-db.json`. Reports gaps for all upcoming trips — no date limit. Supports snooze state. Silent when all bookings are complete or snoozed. Use when the user asks about upcoming travel plans, itinerary completeness, missing reservations, or TripIt trip status.
---

# Check Travel Bookings

Process steps in order. Do not skip ahead. Run the script — do not implement the detection logic yourself.

## Step 1 — Run the script

```bash
python3 /home/node/.claude/skills/tessl__check-travel-bookings/scripts/check-travel-bookings.py
```

The script outputs JSON:
```json
{
  "gaps": [
    {"trip": "JNation 2026", "start": "2026-05-24", "end": "2026-06-01", "issue": "рейсы есть, отеля нет", "slug": "jnation-2026-05", "uncovered_nights": []}
  ],
  "checked_at": "2026-03-28T23:00:00Z",
  "total_trips": 10,
  "complete_trips": 8
}
```

`uncovered_nights` is an array of ISO dates for trips flagged with "нет отеля на N ноч." and an empty array otherwise.

`/workspace/group/travel-db.json` is rebuilt nightly by `tessl__nightly-external-sync` Step 5. Missing/unreadable/invalid DB → exit 1 with `{"error": "..."}` on stdout plus `check-travel-bookings: ...` on stderr. DB alerting is Step 5's responsibility. On non-zero exit, report error output and stop. On invalid JSON or missing fields, report the parse error with raw output.

Proceed immediately to Step 2.

## Step 2 — Interpret and report

If `gaps` is empty, stay silent (proceed silently — no output).

If `gaps` is present, format as Telegram HTML (`parse_mode=HTML`). If conversion needed, pipe through `/workspace/group/scripts/sanitize-html.py` (Markdown → Telegram HTML):

```
<b>Travel bookings to sort out:</b>

• [Trip Name] ([date range]) — [issue]
```

Date range: `May 24–Jun 1` (abbreviated month, no year unless spans years).

If the user is acting on a gap (snooze/resolve), proceed to Step 3. Otherwise finish here.

## Step 3 — Update snooze state

Only run this step when Baruch snoozes or resolves a trip. Update `/workspace/group/travel-booking-state.json`:
- Snooze: set `snooze_until` to a future date for the trip's slug
- Resolved: remove the entry (next nightly rebuild reflects completed bookings)

Slug format: `{normalized-summary}-{YYYY}-{MM}` (lowercase, spaces/punctuation → hyphens).

After writing, verify the file contains valid JSON before confirming. Finish here.
