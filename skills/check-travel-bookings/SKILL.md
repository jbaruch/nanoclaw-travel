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
    {"trip": "JNation 2026", "start": "2026-05-24", "end": "2026-06-01", "issue": "рейсы есть, отеля нет", "slug": "jnation-2026-05", "uncovered_nights": ["2026-05-24", "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29", "2026-05-30", "2026-05-31"]}
  ],
  "checked_at": "2026-03-28T23:00:00Z",
  "total_trips": 10,
  "complete_trips": 8
}
```

`uncovered_nights` lists the ISO dates of trip nights with no lodging coverage. It drives the "нет отеля на N ноч." count; for the "рейсы есть, отеля нет" (no lodging at all) issue it may be empty, and complete trips report an empty array. Which trips are flagged is decided in `skills/check-travel-bookings/scripts/check-travel-bookings.py` — the skill consumes the output, it does not re-derive the selection.

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

Only run this step when Baruch snoozes or resolves a trip. Invoke the bundled mutation script; do not hand-edit `/workspace/group/travel-booking-state.json` directly. The slug-to-trip fuzzy-match (e.g., "snooze JNation" → `jnation-2026-05`) stays in the agent's hands per `coding-policy: script-delegation`; the script handles the deterministic JSON mutation.

```bash
# Snooze a trip until a future date
python3 /home/node/.claude/skills/tessl__check-travel-bookings/scripts/update-travel-booking-state.py \
    --slug <slug> --action snooze --until YYYY-MM-DD

# Resolve (remove the entry; next nightly rebuild reflects the booked state)
python3 /home/node/.claude/skills/tessl__check-travel-bookings/scripts/update-travel-booking-state.py \
    --slug <slug> --action resolve
```

Slug format: `{normalized-summary}-{YYYY}-{MM}` (lowercase, spaces/punctuation → hyphens).

The script emits single-line JSON to stdout `{"action": "...", "slug": "...", "state": {...}}` (the post-update snooze map) on success, or a stderr diagnostic with non-zero exit on validation failure (missing `--until` for snooze, invalid ISO date, etc.). Every snoozed entry the script writes carries `schema_version: 1` per `state-schema.md`. Finish here.
