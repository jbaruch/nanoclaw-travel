# Operator-Local Timezone Phrasing

Phrase every relative-date word in a flight-assist surface against the operator's local date, never the container's UTC clock.

## Scope

- Relative-date words a surface composes relative to "now" — "today", "tomorrow", "a travel day", day-before checks, arrival-logistics summaries
- Governs composed surface text, not the precheck phase-marker / wake-trigger logic
- **Out of scope: clock times.** byAir delivers flight times in the airport's local zone (RFC3339 with offset). Show those as-is. Never convert a displayed departure/arrival time to the operator's zone — this rule changes the today/tomorrow wording only

## Resolve the operator's local date

- Run the core `current-tz` reader, `skills/current-tz/scripts/read-current-tz.py` in `jbaruch/nanoclaw-core` (installed in every tier; the flight-assist step carries the runtime path). It emits `{"available": true, "tz": "<iana>", "local_now": "<ISO-8601>", "local_date": "YYYY-MM-DD"}` or the all-null `available: false` shape
- On `available: true`: `local_date` is the operator's date now
- Run the reader with no arguments in production
- Keep the reader's `--now` seam test-only (core rules/temporal-awareness.md:48-50)
- The precheck converts each departure to the operator's date by passing the reader's `tz` to `phase_markers.day_label` (core current-tz SKILL.md Step 2)
- Never derive a relative date from container-local `datetime.now()`
- Never convert an instant by hand
- A `day_before` event carries `day_label`, resolved by the precheck through the same reader; render it, never re-derive it

## Relative-date phrasing

- `local_event == local_now` → "today"
- `local_event == local_now + 1 day` → "tomorrow"
- Otherwise → an explicit local date (`Sat 5/24`, `в субботу 24-го`)

## Fallback when timezone is unavailable

- `available: false` → phrase with an explicit local date only. The reader's exit code does not change this: exit `1` (the store could not be read) still carries the `available: false` shape on stdout and takes the same explicit-date path
- Never emit a container-UTC-relative "today" / "tomorrow" in the fallback
- No warning marker in the surface
