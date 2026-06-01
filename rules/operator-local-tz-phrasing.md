---
alwaysApply: true
---

# Operator-Local Timezone Phrasing

Phrase every relative-date word in a flight-assist surface against the operator's local date, never the container's UTC clock.

## Scope

- Relative-date words a surface composes relative to "now" — "today", "tomorrow", "a travel day", day-before checks, arrival-logistics summaries
- Governs composed surface text, not the precheck phase-marker / wake-trigger logic
- **Out of scope: clock times.** byAir delivers flight times in the airport's local zone (RFC3339 with offset). Show those as-is. Never convert a displayed departure/arrival time to the operator's zone — this rule changes the today/tomorrow wording only

## Resolve the operator's local date

- Run `skills/flight-assist/scripts/read-current-tz.py`. It emits `{"available": true, "tz": "<iana>"}` or `{"available": false, "tz": null}`
- On `available: true`: `local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(tz)).date()`. Each event's local date the same way from its `scheduled_dep_time`: `dep_dt.astimezone(ZoneInfo(tz)).date()`
- Never derive a relative date from container-local (UTC) `datetime.now()`

## Relative-date phrasing

- `local_event == local_now` → "today"
- `local_event == local_now + 1 day` → "tomorrow"
- Otherwise → an explicit local date (`Sat 5/24`, `в субботу 24-го`)

## Fallback when timezone is unavailable

- `available: false` → phrase with an explicit local date only
- Never emit a container-UTC-relative "today" / "tomorrow" in the fallback
- No warning marker in the surface
