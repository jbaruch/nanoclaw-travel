# Check Travel Bookings — State Schema

This skill owns two cross-invocation JSON state artifacts under `/workspace/group/`. Per `coding-policy: stateful-artifacts`, both carry a `schema_version` field for auditable migration. `travel-db.json` is at **3**; `travel-booking-state.json` is at **1**.

## `/workspace/group/travel-db.json`

Compact day-indexed projection of upcoming trips.

- **Owner skill:** `check-travel-bookings` (this skill)
- **Writer:** `scripts/build-travel-db.py` (invoked by this plugin's `nightly-travel-sync` Step 4 via the literal plugin-mount path `/home/node/.claude/skills/tessl__check-travel-bookings/scripts/build-travel-db.py`)
- **Readers:**
  - `scripts/check-travel-bookings.py` (owner; gates on `schema_version`)
  - `nanoclaw-admin/morning-brief` (cross-plugin, via the same script invoked as the reader)
  - `flight-assist/trip_window.py` (same-plugin non-owner reader — the #147 trip-window gate). Gates on `schema_version` and, per `coding-policy: stateful-artifacts`, treats any version outside its accepted set as no-usable-state and **fails open** (defers to the host pre-spawn gate rather than blind a possibly-active trip). A bump here lands in lock-step with `trip_window._ACCEPTED_TRAVEL_DB_SCHEMA_VERSIONS`.
  - `nightly-travel-sync/precheck.py` (same-plugin non-owner reader — the #268 schema gate). Reads `schema_version` alone, never the body, and never writes or migrates. A stamp below its `EXPECTED_DB_SCHEMA_VERSION` wakes the bundle so Step 4 rebuilds at the current schema; an unreadable or unstamped DB reads the same way. A stamp above it means the precheck is the lagging side, so it defers to its own age cap rather than driving the writer into the refuse-to-downgrade guard below. A bump here lands in lock-step with that constant, which `tests/test_nightly_travel_sync_precheck.py` asserts against `build-travel-db.py`'s `SCHEMA_VERSION`.
  - The host gate `src/host-plugins/flight-assist-spawn-gate.ts` in jbaruch/nanoclaw reads this file through its own pipeline, but reads **only** trip-level `start`/`end` and never `schema_version`, so a version bump here is invisible to it. A change to the trip-level shape is what would need lock-step there.
- **Schema:**

```json
{
  "schema_version": 3,
  "generated_at": "YYYY-MM-DDTHH:MM:SSZ",
  "trips": {
    "<slug>": {
      "summary": "...",
      "start": "YYYY-MM-DD",
      "end": "YYYY-MM-DD",
      "destination": "Nashville, TN",
      "days": { "YYYY-MM-DD": [<item>, ...] }
    }
  }
}
```

Each `<item>` carries `type`, `summary`, `start`, `end`, `uid`, and — for a timed record whose local clock resolved — the optional `start_local` / `end_local` stamps (`YYYY-MM-DDTHH:MM:SS±HH:MM`) carried through from `travel-schedule.json` v3. The `days` key is the item's LOCAL date when it has one, its UTC date otherwise. Date-granular readers take the local field first and fall back to the UTC one; see `scripts/check-travel-bookings.py:_item_day`.

An item files under a trip by the same local-first days. A transport item (`Flight`, `Rail`) is a point event and appears under a trip only when its departure day is inside the trip's `[start, end]`; every other item type appears under each trip its `[start day, end day]` span overlaps. A transport item on a boundary day two adjacent trips share appears under both. The writer's `_belongs_to_trip` is the rule; no `schema_version` change accompanied it (#293).

`destination` is the trip wrapper's TripIt primary location (`<City>, <Region>`) as `travel-schedule.json` carries it — decoded at that writer since its v4 (#275) — and is optional: it is written only when the feed labels the trip. An absent `destination` means the destination is unknown, which no reader may treat as home.

### v2 → v3

Additive (#271): trips gain the optional `destination` field. A v2 DB reads with it simply absent, so `check-travel-bookings.py` and `trip_window.py` both accept `{1, 2, 3}` for the rollout window; `precheck.py`'s schema gate wakes the bundle on a v2 stamp so the next `nightly-travel-sync` fire closes the window rather than waiting for the DB to age past the cadence cap. The field is what separates a local placeholder trip — one the operator files to block time for a home-metro event, with nothing to book — from an away trip that genuinely has no bookings yet; the empty-itinerary signal alone cannot tell them apart.

### v1 → v2

Additive (#268): day items gain the optional `start_local` / `end_local` fields, and the `days` key follows the local date where one exists. Trip-level `start`/`end` are unchanged. A v1 DB reads with the local fields simply absent, so `check-travel-bookings.py` and `trip_window.py` both accept `{1, 2}` for the rollout window. The rollout window closes on the first `nightly-travel-sync` fire after the bump ships: `precheck.py`'s schema gate wakes the bundle on a v1 stamp rather than waiting for the DB to age past the cadence cap (#268). Drop `1` from both accepted sets once no v1 DB can be in play.

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
- `schema_version` outside the reader's accepted set is treated as forward-incompatible — `check-travel-bookings.py` returns no-prior-state and `build-travel-db.py` does not overwrite. `travel-booking-state.json` entries follow the same gate.
- Non-owner readers MUST treat a `schema_version` mismatch as no-prior-state without rewriting. There are two, both same-plugin (see the `travel-db.json` readers list above): `flight-assist/trip_window.py` (the #147 trip-window gate) fails open on any stamp outside its accepted set, so a bump runs mixed versions until `trip_window._ACCEPTED_TRAVEL_DB_SCHEMA_VERSIONS` advances; `nightly-travel-sync/precheck.py` (the #268 schema gate) wakes the bundle on a stamp below its own constant so the writer rebuilds. Neither writes. Writer and readers ship in one plugin, so a bump lands with its dual-accept readers in the same release.

## Schema-version constant

Defined in `scripts/build-travel-db.py` (writer) and `scripts/check-travel-bookings.py` (reader) as `SCHEMA_VERSION = 3`. Bump in lock-step when changing the on-disk shape, and widen the readers' accepted sets in the same change.
