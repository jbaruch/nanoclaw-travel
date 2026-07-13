# Check Travel Bookings — State Schema

This skill owns two cross-invocation JSON state artifacts under `/workspace/group/`. Per `coding-policy: stateful-artifacts`, both carry a `schema_version` field for auditable migration. The current schema version is **1**.

## `/workspace/group/travel-db.json`

Compact day-indexed projection of upcoming trips.

- **Owner skill:** `check-travel-bookings` (this skill)
- **Writer:** `scripts/build-travel-db.py` (invoked by this plugin's `nightly-travel-sync` Step 4 via the literal plugin-mount path `/home/node/.claude/skills/tessl__check-travel-bookings/scripts/build-travel-db.py`)
- **Readers:**
  - `scripts/check-travel-bookings.py` (owner; gates on `schema_version`)
  - `nanoclaw-admin/morning-brief` (cross-plugin, via the same script invoked as the reader)
  - `flight-assist/trip_window.py` (cross-plugin non-owner reader — the #147 trip-window gate). Gates on `schema_version` and, per `coding-policy: stateful-artifacts` Cross-Pipeline Schema Bumps, treats any version other than the accepted `1` as no-usable-state and **fails open** (defers to the host pre-spawn gate rather than blind a possibly-active trip). A bump here must land in lock-step with `trip_window._ACCEPTED_TRAVEL_DB_SCHEMA_VERSION`, and with the host gate `src/spawn-gates.ts` in jbaruch/nanoclaw, which read the same file through their own pipelines.
- **Schema:**

```json
{
  "schema_version": 1,
  "generated_at": "YYYY-MM-DDTHH:MM:SSZ",
  "trips": {
    "<slug>": {
      "summary": "...",
      "start": "YYYY-MM-DD",
      "end": "YYYY-MM-DD",
      "days": { "YYYY-MM-DD": [<item>, ...] }
    }
  }
}
```

## `/workspace/group/travel-booking-state.json`

Per-trip snooze and resolve markers for surfacing in `check-travel-bookings` and `morning-brief`.

- **Owner skill:** `check-travel-bookings` (this skill)
- **Writer:** `scripts/update-travel-booking-state.py` (invoked by SKILL.md Step 3). The script stamps `schema_version: 1` on every written entry.
- **Reader:** `scripts/check-travel-bookings.py`
- **Schema:**

```json
{
  "<slug>": {
    "schema_version": 1,
    "snooze_until": "YYYY-MM-DD"
  }
}
```

A `resolved` outcome is represented by removing the entry entirely (the next nightly rebuild reflects the booked state).

## Migration policy

- The owner skill migrates on its own read: legacy data without `schema_version` is treated as implicit v1 (the schema was introduced at v1; no prior version exists). Subsequent writes stamp the field explicitly.
- `schema_version` higher than the current constant (currently 1) is treated as forward-incompatible — `check-travel-bookings.py` returns no-prior-state and `build-travel-db.py` does not overwrite.
- Non-owner readers MUST treat a `schema_version` mismatch as no-prior-state without rewriting. Today's cross-plugin non-owner reader is `flight-assist/trip_window.py` (the #147 trip-window gate; see the `travel-db.json` readers list above) — it accepts only version `1` and fails open on any other stamp, so a bump here runs mixed versions until `trip_window._ACCEPTED_TRAVEL_DB_SCHEMA_VERSION` (and the host gate `src/spawn-gates.ts` in jbaruch/nanoclaw) are advanced in lock-step.

## Schema-version constant

Defined in `scripts/build-travel-db.py` (writer) and `scripts/check-travel-bookings.py` (reader) as `SCHEMA_VERSION = 1`. Bump in lock-step when changing the on-disk shape.
