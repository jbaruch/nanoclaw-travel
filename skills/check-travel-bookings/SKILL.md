---
name: check-travel-bookings
description: Checks upcoming trips for missing bookings (flights, hotels, accommodation) by reading the nightly-built `travel-db.json`, and warns when a hotel stay's TripIt location is a garbage value (a fee/rate note or blank) that needs a manual TripIt edit. Reports gaps for all upcoming trips — no date limit. Supports snooze state. Silent when all bookings are complete and every hotel location is a plausible address (or snoozed). Use when the user asks about upcoming travel plans, itinerary completeness, missing reservations, hotel address problems, or TripIt trip status.
---

# Check Travel Bookings

Process steps in order. Do not skip ahead. Run the script — do not implement the detection logic yourself.

## Step 1 — Run the booking-gap script

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

`uncovered_nights` lists the ISO dates of trip nights with no lodging coverage. It drives the "нет отеля на N ноч." count. For the "рейсы есть, отеля нет" issue it may be empty. Complete trips report an empty array. Trip selection lives in `skills/check-travel-bookings/scripts/check-travel-bookings.py`. The skill consumes that output and does not re-derive it.

The "отель есть, рейса нет" issue is the mirror case — a hotel booked with nothing to get there on. It fires only for a trip the drive engine has settled as a flight, since a trip the operator drives to has no transport booking by design. That verdict is read from drive-engine's `drive-decisions.json` (`load_flying_trips`; owner contract in `skills/drive-engine/state-schema.md`). Do not re-derive it — an absent or unreadable store means no such gap.

`/workspace/group/travel-db.json` is rebuilt nightly by `tessl__nightly-travel-sync` Step 4. Missing/unreadable/invalid DB → exit 1 with `{"error": "..."}` on stdout plus `check-travel-bookings: ...` on stderr. DB alerting is Step 4's responsibility. On non-zero exit, report error output and stop. On invalid JSON or missing fields, report the parse error with raw output.

Proceed immediately to Step 2.

## Step 2 — Run the hotel-location check

```bash
python3 /home/node/.claude/skills/tessl__check-travel-bookings/scripts/check-lodging-locations.py
```

The script outputs JSON:
```json
{
  "garbage_lodging": [
    {"hotel": "Hilton Amsterdam Airport Schiphol", "location": "Stay resort fee: $72.03", "checkin": "2026-09-07", "reason": "location looks like a fee/rate note, not an address"}
  ],
  "checked_at": "2026-08-02T15:00:00Z"
}
```

Each entry is an upcoming stay whose TripIt `location` is not a usable address — a fee/rate note or a blank — which breaks drive planning and reads as nonsense on the calendar. Detection lives in `scripts/check-lodging-locations.py`; the skill consumes the output and does not re-derive it. The script always exits 0 (a missing/unreadable schedule yields an empty `garbage_lodging` list — schedule freshness is `nightly-travel-sync`'s alert surface, not this check's).

Proceed immediately to Step 3.

## Step 3 — Interpret and report

If `gaps` AND `garbage_lodging` are both empty, stay silent (proceed silently — no output).

Report each present block as Telegram HTML (`parse_mode=HTML`). If conversion is needed, pipe through `/workspace/group/scripts/sanitize-html.py` (Markdown → Telegram HTML).

**Booking gaps** (`gaps` non-empty):

```
<b>Travel bookings to sort out:</b>

• [Trip Name] ([date range]) — [issue]
```

Date range: `May 24–Jun 1` (abbreviated month, no year unless spans years).

**Hotel-location warnings** (`garbage_lodging` non-empty) — the fix is a manual TripIt edit, so name the hotel and the bad value and tell the operator to correct the address in TripIt:

```
<b>⚠️ Hotel location needs a TripIt fix:</b>

• [hotel] (check-in [date]) — [reason]: "[location]"
```

Relay the script's `location` and `reason` verbatim. When both blocks are present, send them together (one message, two labelled sections). If the user is acting on a gap (snooze/resolve), proceed to Step 4. Otherwise finish here.

## Step 4 — Update snooze state

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
