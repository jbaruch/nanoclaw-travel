# Flight-Assist State Schema

Documents the on-disk state files the flight-assist tile reads and writes. Per `coding-policy: stateful-artifacts`.

## Owner Skill

`flight-assist` (this tile) is the sole owner. Only this skill migrates `schema_version`. Reader skills (other tiles, agent-side composition, sync-tripit) call the snapshot reader API — `read_active_flights_snapshot` / `read_flight_state_snapshot` — which treats a `schema_version` strictly below the current `STATE_SCHEMA_VERSION` as "no usable prior state" and returns without rewriting the file. A `schema_version` ABOVE the current still raises `StateError` from any reader (forward incompatibility — operators must upgrade the consumer tile, not be told there's nothing on disk).

## State Directory

- Production: `/workspace/state/flight-assist/`
- Tests override via `FLIGHT_ASSIST_STATE_DIR` environment variable

## Files

### `config.json`

Tile-wide configuration set during install via the `/setup` flow.

```json
{
  "schema_version": 3,
  "home_address": "1 Infinite Loop, Cupertino, CA 95014",
  "min_transfer_minutes": 45
}
```

Fields:

- `schema_version` (int, required) — currently `3`
- `home_address` (string, optional) — origin used for the time-to-leave capability when no other location is known
- `min_transfer_minutes` (int, optional) — overrides `connection_risk.DEFAULT_MIN_TRANSFER_MINUTES` (45) for the connection-risk capability. Set higher for travellers who routinely connect through hubs with longer minimum connect times (LHR, FRA, JFK with terminal change)

### `active-flights.json`

Index of currently-tracked flight IDs. Refreshed daily by the sync-tripit script.

```json
{
  "schema_version": 3,
  "flight_ids": [12345, 67890, 11111]
}
```

Fields:

- `schema_version` (int, required) — currently `3`
- `flight_ids` (list of int, required) — every flight the precheck should poll

### `current-location.json`

Latest known user location. **Owner is the host orchestrator**, not flight-assist — the host writes this file as Telegram live-location updates and message metadata arrive. flight-assist is a non-owner reader per `coding-policy: stateful-artifacts`: it consults the snapshot to resolve the time-to-leave origin (ladder lives in `precheck._resolve_time_to_leave_origin`), validates the documented shape, and returns `None` on any mismatch instead of raising or migrating.

```json
{
  "schema_version": 1,
  "latitude": 59.6519,
  "longitude": 17.9186,
  "captured_at": "2026-05-20T11:42:11Z"
}
```

Fields:

- `schema_version` (int, required) — currently `1`. Tracked separately from `STATE_SCHEMA_VERSION`. The host owns version bumps. `read_current_location` requires equality with `state.CURRENT_LOCATION_SCHEMA_VERSION` and returns `None` on any mismatch
- `latitude` (float, required) — degrees in `[-90, 90]`
- `longitude` (float, required) — degrees in `[-180, 180]`
- `captured_at` (RFC 3339 UTC, required) — when the orchestrator observed the location; consumers apply their own freshness window

Origin-resolution ladder used by `precheck._resolve_time_to_leave_origin`:

1. Fresh snapshot — `now - captured_at <= 30 min` → formatted `"<latitude>,<longitude>"` for the Distance Matrix API
2. `home_address` from `config.json`
3. `None` — caller skips the maps query

A `schema_version` mismatch (host bumped) returns `None` from the reader and the precheck falls back to `home_address`, matching the no-snapshot-on-disk path.

### `flight-<flight_id>.json`

Per-flight state record. One file per tracked flight.

```json
{
  "schema_version": 3,
  "flight_id": 12345,
  "code": "AA2414",
  "ownership": "mine",
  "trip_id": 678,
  "scheduled_dep_time": "2026-05-17T09:00:00-07:00",
  "scheduled_arr_time": "2026-05-17T11:09:00-07:00",
  "dep_airport_id": 20,
  "arr_airport_id": 28,
  "last_polled_at": "2026-05-17T18:42:11Z",
  "last_snapshot": {
    "code": "AA2414",
    "computed_status": "boarding",
    "computed_status_detail": "Boarding starts in 36min",
    "computed_phase_progress": 0,
    "computed_phase_risk": "low",
    "computed_phase_overdue": null,
    "dep_gate": "B25",
    "arr_gate": "A26",
    "dep_terminal": "1",
    "arr_terminal": "2",
    "dep_time": "2026-05-17T13:00:00-07:00",
    "arr_time": "2026-05-17T15:02:00-07:00",
    "baggage": null,
    "inbound": {
      "aircraft_model": "Airbus A320",
      "registration": "N660AW",
      "flew": false,
      "predicted_delay_minutes": null
    },
    "position_lat": 37.617678,
    "position_lon": -122.380227
  },
  "phase_markers": {
    "day_before_fired": false,
    "time_to_leave_fired": false,
    "boarding_fired": false,
    "arrival_logistics_fired": false,
    "landed_acknowledged": false,
    "connection_at_risk_fired": false
  },
  "last_wake_at": null,
  "last_wake_reason": null,
  "calendar_events": {
    "boarding": {
      "event_id": "abc123def456",
      "calendar_id": "<byair-calendar-id resolved at runtime>",
      "managed": "created",
      "synced_signature": "2026-05-17T12:24:00-07:00/2026-05-17T13:00:00-07:00"
    },
    "flight": {
      "event_id": "ghi789jkl012",
      "calendar_id": "<byair-calendar-id resolved at runtime>",
      "managed": "adopted",
      "synced_signature": "2026-05-17T13:00:00-07:00/2026-05-17T15:02:00-07:00"
    }
  }
}
```

Top-level fields:

- `schema_version` (int, required) — `3`
- `flight_id` (int, required) — byAir's flight identifier
- `code` (string, required) — flight number like `"AA2414"`
- `ownership` (string, required) — `"mine"` or `"friend"`
- `trip_id` (int, required) — byAir's trip identifier (groups multi-leg trips)
- `scheduled_dep_time`, `scheduled_arr_time` (RFC 3339 with offset, required)
- `dep_airport_id`, `arr_airport_id` (int, required) — byAir airport IDs
- `last_polled_at` (RFC 3339 UTC, required) — wall-clock time of the last byAir fetch; cadence-gating consults this
- `last_snapshot` (object, optional) — the slim ~1KB operational slice from byAir; `null` on the first run before the precheck has fetched anything
- `phase_markers` (object, required) — once-per-flight fire-and-forget gates for time-based wakes
- `last_wake_at` (RFC 3339 UTC, optional) — when the agent was last woken for this flight
- `last_wake_reason` (string, optional) — the most recent wake reason for debug
- `calendar_events` (object, optional) — the ledger of flight-assist-owned/adopted Google Calendar events for this flight, keyed by event kind. Absent or `{}` means none are tracked yet. Validated structurally (object) by `state.py`; the per-entry shape below is owned and deep-validated by the calendar-reconcile planner (`calendar_plan.py`) — the same split as `last_snapshot` ↔ `byair_client`. The map is the source of truth for O(1) update/delete and doubles as the teardown tombstone when a flight leaves `active-flights.json` (the reconciler still holds the event IDs after the flight is gone)

`calendar_events` entries — one per kind, kind ∈ `{boarding, flight}`:

- `boarding` — the flight-assist-created boarding block (boarding-start → departure). `managed` is always `"created"`; flight-assist owns its full lifecycle (create / shift / delete)
- `flight` — the byAir-created flight event flight-assist adopted by tagging. `managed` is always `"adopted"`; flight-assist shifts it (delta-only) and deletes it on a true switch/cancel byAir left stale, but never casually

Each entry's fields:

- `event_id` (string, required) — the Google Calendar event identifier
- `calendar_id` (string, required) — the calendar the event lives in (the byAir calendar for both the boarding block and adopted flight events; byAir writes the flight events there and flight-assist places the boarding block alongside them). The byAir calendar's ID is resolved at runtime via Composio from the operator's flights calendar — never hardcoded in the tile
- `managed` (string, required) — `"created"` (flight-assist authored it) or `"adopted"` (flight-assist tagged a byAir-authored event). Drives delete semantics: `created` is freely deletable, `adopted` is deleted only on a true switch/cancel
- `synced_signature` (string, required) — the `<start>/<end>` instant pair flight-assist last wrote, so the planner can no-op when the live event already matches byAir truth instead of re-writing every cycle

`last_snapshot` fields (mirrors the post-filter byAir slice — see `byair_client.py`'s `get_flight()` output; this dict is what `wake_rules.py` will diff against in PR #6):

- `code` — flight number
- `computed_status` — enum: `"scheduled"`, `"check_in_open"`, `"boarding"`, `"departed"`, `"en_route"`, `"landed"`, `"cancelled"`, `"diverted"`
- `computed_status_detail` — human-readable phase prose ("Departing in 3h 20min")
- `computed_phase_progress` (float 0..1, optional) — elapsed fraction of the current phase
- `computed_phase_risk` (string, optional) — `"low"`/`"warning"`/`"danger"`; only for `check_in_open` and `boarding`
- `computed_phase_overdue` (bool, optional) — `true` when departed/en_route is overdue
- `dep_gate`, `arr_gate` (string, optional) — gate numbers as they appear on the board
- `dep_terminal`, `arr_terminal` (string, optional)
- `dep_time`, `arr_time` (RFC 3339 with offset, optional) — actual times, may be ahead of or behind scheduled
- `baggage` (string, optional) — baggage carousel claim once revealed
- `inbound` (object, optional) — Find My Plane data: `aircraft_model`, `registration`, `flew`, `predicted_delay_minutes`
- `position_lat`, `position_lon` (float, optional) — last known aircraft position

`phase_markers` booleans — `true` means the corresponding time-based wake event has already fired and won't fire again for this flight:

- `day_before_fired` — T-24h sanity check
- `time_to_leave_fired` — Traffic-aware leave-by alert
- `boarding_fired` — Status transition to `boarding`
- `arrival_logistics_fired` — T-arr−15min logistics push
- `landed_acknowledged` — User acknowledged the landing notification
- `connection_at_risk_fired` — Cross-flight: projected transfer window on this leg-2 has fallen below `min_transfer_minutes`. Carried on the leg-2 (downstream) record so the marker survives leg-1 landing

## Atomic Writes

Every `write_*` helper uses write-to-tmp + `os.replace` in the same directory so a kill mid-write doesn't leave a half-written file. Cross-device renames are not atomic — the state dir must live on a single filesystem.

## Migration Policy

Today `STATE_SCHEMA_VERSION` is `3`.

`state.py`'s read helpers enforce these rules on `schema_version`:

- Equal to current → return the payload
- Higher than current → `StateError` (forward incompatibility; never silently downgrade)
- Lower than current → run the owner-side migration in `_migrate`, rewrite at the current version, return the upgraded payload
- Missing, wrong type (non-int, including `bool`) → `StateError` with actionable repair message

Non-owner readers (sync-tripit, future cross-tile composition) call the dedicated snapshot entry points: `read_active_flights_snapshot()` and `read_flight_state_snapshot(flight_id)`. These mirror the owner-side functions' return shapes but treat a `schema_version` strictly LESS THAN `STATE_SCHEMA_VERSION` as "no usable prior state" (return `[]` / `None`) without invoking `_migrate`. A `schema_version` ABOVE the current still raises `StateError` from the snapshot path (forward incompatibility); so does corrupt JSON or a missing required field at the current schema. Only the owner skill (`flight-assist`, this tile, via `state.py:_migrate`) migrates.

Migrations chain: `state.py:_migrate` steps a record through every intermediate version in one call (a v1 record runs v1→v2→v3 before returning), so an old file lands at the current version on its first owner-side read.

### v1 → v2

Per-flight state: `phase_markers` gains `connection_at_risk_fired: false`. The owner-side migration in `state.py:_migrate` adds the missing key on first read and rewrites the file at v2. Config and active-flights files have no shape change at v2 — they receive a schema_version bump only.

### v2 → v3

Per-flight state: gains the `calendar_events` map (empty `{}` on migration). The owner-side migration in `state.py:_migrate` adds the missing key on first read — scoped by the `flight-<id>.json` filename, not by payload contents, so a config/active-flights file (or any future record that happens to carry a `flight_id` key) is never given this per-flight-only field — and rewrites the file at v3. Config and active-flights files have no shape change at v3 — they receive a schema_version bump only.

## Bump Procedure

When adding or renaming a field:

1. Bump `STATE_SCHEMA_VERSION` in `state.py` and document the new shape in this file
2. Add migration logic to `state.py` that reads old `schema_version` and rewrites the upgraded shape via the owner-skill code path (the precheck, the agent on wake, sync-tripit — every entry point that uses `read_flight_state` from inside `flight-assist`)
3. Non-owner reader skills (other tiles, future cross-tile composition) call `read_active_flights_snapshot` / `read_flight_state_snapshot` instead of the owner-side helpers; the snapshot readers treat any mismatched `schema_version` as "no usable prior state" and return without rewriting. Migration happens exclusively on the owner-skill's next read
4. CHANGELOG entry describing the version bump and the owner-side migration path — an un-headed `### ` block at the top of `CHANGELOG.md`; the publish stamp step adds the `## <version> — <date>` heading (no `## Unreleased` section — that heading is forbidden per `coding-policy: context-artifacts` CHANGELOG Hygiene)
