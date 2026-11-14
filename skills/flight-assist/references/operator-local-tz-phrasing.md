# Operator-Local Timezone Phrasing

Phrase every relative-date word in a flight-assist surface against the operator's local date, never the container's UTC clock.

## Scope

- Relative-date words a surface composes relative to "now" — "today", "tomorrow", "a travel day", day-before checks, arrival-logistics summaries
- Governs composed surface text and script-computed date labels, not phase-marker wake thresholds
- **Out of scope: clock times.** byAir delivers flight times in the airport's local zone (RFC3339 with offset). Show those as-is. Never convert a displayed departure/arrival time to the operator's zone — this rule changes the today/tomorrow wording only

## Resolve the operator's local date

- Run the core `current-tz` reader at `/home/node/.claude/skills/tessl__current-tz/scripts/read-current-tz.py` in `jbaruch/nanoclaw-core`, installed in every tier. It emits `{"available": true, "tz": "<iana>", "local_now": "<ISO-8601>", "local_date": "YYYY-MM-DD"}` or the all-null `available: false` shape
- When composing wording without a script-computed label, on `available: true` use `local_date` as the operator's date now. Get each event's local date from the same script run with `--now <scheduled_dep_time>`; read its `local_date`
- Never derive a relative date from container-local `datetime.now()`
- The model never converts an instant by hand

## Script-computed labels

- Core `current-tz` remains the sole authority for the operator's current zone. Scripts never open its backing store or substitute `home_tz` or the container zone.
- `skills/flight-assist/phase_markers.py` — `day_label` owns the deterministic `day_before` label contract. The precheck obtains the current zone through `skills/travel-core/operator_tz.py` — `read_operator_tz`, lazily and at most once per cycle, shared by all due flights.
- With a resolvable reader-returned zone, `day_label` converts its aware departure and injected `now_utc` through `ZoneInfo` and compares their calendar dates. It does not spawn the reader for each event. This script conversion is permitted; model conversion is forbidden.
- A `day_before` event carries `day_label` and `day_label_tz`; render the label verbatim, never re-derive it. `skills/flight-assist/references/event-payloads.md` documents the payload.
- Without a usable zone, `day_label` returns the explicit date in the departure's supplied airport-local offset and a null `day_label_tz`. An unresolvable zone also emits a stderr diagnostic.

## Relative-date phrasing

- `local_event == local_now` → "today"
- `local_event == local_now + 1 day` → "tomorrow"
- Otherwise → an explicit local date (`Sat 5/24`, `в субботу 24-го`)

## Fallback when timezone is unavailable

- `available: false` → phrase with an explicit local date only. The reader's exit code does not change this: exit `1` (the store could not be read) still carries the `available: false` shape on stdout and takes the same explicit-date path
- Never emit a container-UTC-relative "today" / "tomorrow" in the fallback
- No warning marker in the surface
