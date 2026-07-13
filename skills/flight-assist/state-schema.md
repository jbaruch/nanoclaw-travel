# Flight-Assist State Schema

Documents the on-disk state files the flight-assist plugin reads and writes. Per `coding-policy: stateful-artifacts`.

## Owner Skill

`flight-assist` (this plugin) is the sole owner. Only this skill migrates `schema_version`. Reader skills (other plugins, agent-side composition, sync-tripit) call the snapshot reader API — `read_active_flights_snapshot` / `read_flight_state_snapshot` — which treats ANY `schema_version` other than the current `STATE_SCHEMA_VERSION` as "no usable prior state" and returns without rewriting the file: below, the owner hasn't migrated the file yet; above, the reading plugin is lagging behind the owner mid-rollout and degrades safely instead of wedging. Owner-side reads stay strict — a `schema_version` above the current raises `StateError` from the owner path only (the owner must never run behind its own state files). See Migration Policy below.

## State Directory

- Production: `/workspace/state/flight-assist/`
- Tests override via `FLIGHT_ASSIST_STATE_DIR` environment variable

## Files

### `config.json`

Plugin-wide configuration set during install via the `/setup` flow.

```json
{
  "schema_version": 6,
  "home_address": "1 Infinite Loop, Cupertino, CA 95014",
  "min_transfer_minutes": 45,
  "byair_calendar_name": "Flighty Flights",
  "byair_calendar_id": "c_abc123@group.calendar.google.com"
}
```

Fields:

- `schema_version` (int, required) — currently `6`
- `home_address` (string, optional) — origin used for the time-to-leave capability when no other location is known
- `min_transfer_minutes` (int, optional) — overrides `connection_risk.DEFAULT_MIN_TRANSFER_MINUTES` (45) for the connection-risk capability. Set higher for travellers who routinely connect through hubs with longer minimum connect times (LHR, FRA, JFK with terminal change)
- `byair_calendar_name` (string, optional) — display name of the operator's flight calendar (the byAir calendar in plugin terms; the operator's is literally titled "Flighty Flights"). Operator-supplied data, not hardcoded in plugin code per `rules/flight-data-locality.md`. The calendar `reconcile` script matches this name against the live calendar list once to resolve the calendar ID. Absent → calendar reconciliation no-ops (no flight calendar to write to)
- `byair_calendar_id` (string, optional) — the resolved Google Calendar ID for the flight calendar, cached by `reconcile` after its first name match so later cycles skip the lookup. When present it is used directly and `byair_calendar_name` is not consulted. The Reclaim travel blocks live on the **primary** calendar (content-classified — there is no dedicated Reclaim calendar), so no config field tracks it
- `airport_clearance_domestic_minutes` (int, optional, non-negative) — minutes before departure to be AT the airport for a domestic (incl. intra-Schengen) flight; overrides `airport_lead.BASE_CLEARANCE_DOMESTIC_MINUTES` (60). Airport drive blocks (#90)
- `airport_clearance_international_minutes` (int, optional, non-negative) — same, international flight; overrides `airport_lead.BASE_CLEARANCE_INTERNATIONAL_MINUTES` (120)
- `airport_post_arrival_domestic_minutes` (int, optional, non-negative) — minutes after landing before the drive home can start, domestic arrival; overrides `airport_lead.POST_ARRIVAL_DOMESTIC_MINUTES` (20)
- `airport_post_arrival_intl_us_minutes` (int, optional, non-negative) — same, international arrival INTO the US; overrides `airport_lead.POST_ARRIVAL_INTL_TO_US_MINUTES` (40)
- `airport_post_arrival_intl_abroad_minutes` (int, optional, non-negative) — same, international arrival abroad; overrides `airport_lead.POST_ARRIVAL_INTL_ABROAD_MINUTES` (60). The byAir `delay.index` nudge (low/med/high → +0/+15/+30) stays an `airport_lead` constant, not a config field

### `active-flights.json`

Index of currently-tracked flight IDs. Refreshed daily by the sync-tripit script.

```json
{
  "schema_version": 6,
  "flight_ids": [12345, 67890, 11111]
}
```

Fields:

- `schema_version` (int, required) — currently `6`
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
  "schema_version": 6,
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
    "dep_airport_code": "PHX",
    "dep_airport_name": "Phoenix Sky Harbor International Airport",
    "arr_airport_code": "SFO",
    "arr_airport_name": "San Francisco International Airport",
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
    "connection_at_risk_fired": false,
    "gate_assignment_fired": false
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

- `schema_version` (int, required) — `6`
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
- `calendar_events` (object, optional) — the ledger of flight-assist-owned/adopted Google Calendar events for this flight, keyed by event kind. Absent or `{}` means none are tracked yet. Validated structurally (object) by `state.py`; the per-entry shape below is owned and deep-validated by the calendar-reconcile planner (`calendar_plan.py`) — the same split as `last_snapshot` ↔ `byair_client`. The map is the source of truth for O(1) update/delete and doubles as the teardown tombstone when a flight leaves `active-flights.json` (the reconciler still holds the event IDs after the flight is gone). Tombstone lifecycle: when a flight drops from the upstream index, `sync_tripit` retains `flight-<id>.json` instead of deleting it if this map is non-empty; the reconcile sweep then deletes the managed events off the ledger and archives (removes) the state file once teardown settles — every teardown delete succeeded, or the flight has completed and its events are left as a record. A failed delete keeps its entry, so the tombstone is retained for the next cycle's retry

`calendar_events` entries — one per kind, kind ∈ `{boarding, flight}`:

- `boarding` — the flight-assist-created boarding block (boarding-start → departure). `managed` is always `"created"`; flight-assist owns its full lifecycle (create / shift / delete)
- `flight` — the byAir-created flight event flight-assist adopted by tagging. `managed` is always `"adopted"`; flight-assist shifts it (delta-only) and deletes it on a true switch/cancel byAir left stale, but never casually

Each entry's fields:

- `event_id` (string, required) — the Google Calendar event identifier
- `calendar_id` (string, required) — the calendar the event lives in (the byAir calendar for both the boarding block and adopted flight events; byAir writes the flight events there and flight-assist places the boarding block alongside them). The byAir calendar's ID is resolved at runtime via Composio from the operator's flights calendar — never hardcoded in the plugin
- `managed` (string, required) — `"created"` (flight-assist authored it) or `"adopted"` (flight-assist tagged a byAir-authored event). Drives delete semantics: `created` is freely deletable, `adopted` is deleted only on a true switch/cancel
- `synced_signature` (string, required) — the `<start>/<end>` instant pair flight-assist last wrote, so the planner can no-op when the live event already matches byAir truth instead of re-writing every cycle

`last_snapshot` fields (mirrors the post-filter byAir slice — see `byair_client.py`'s `get_flight()` output; this dict is what `wake_rules.py` will diff against in PR #6):

- `code` — flight number
- `dep_airport_code`, `arr_airport_code` (string, optional) — the departure/arrival airport IATA/display code (e.g. `"JFK"`, `"BNA"`), captured off the byAir flight payload's `depAirport`/`arrAirport`. The compose step renders the airport from these resolved values rather than free-typing a name off the numeric `dep_airport_id`/`arr_airport_id` alone (#159 Bug 2)
- `dep_airport_name`, `arr_airport_name` (string, optional) — the departure/arrival airport display name (e.g. `"Nashville International Airport"`), captured off the same payload. Paired with the codes so the compose has a human name without a second lookup
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
- `aircraft_model` (string, optional) — byAir's top-level aircraft model. Consumed by the calendar `reconcile` boarding-lead resolver (`boarding_lead.py`), which falls back to `inbound.aircraft_model` when this is absent or empty
- `dep_lat`, `dep_lon`, `arr_lat`, `arr_lon` (float, optional) — departure/arrival **airport** coordinates (distinct from `position_lat`/`position_lon`, which track the aircraft). The boarding-lead resolver uses them for the transoceanic (TATL/TPAC) check; when any is absent it skips that check and falls back to aircraft size. Populated once the byAir field names for top-level model and airport coordinates are confirmed (#55 runtime facts); until then the resolver runs on `inbound.aircraft_model` and the narrowbody default

`phase_markers` booleans — `true` means the corresponding time-based wake event has already fired and won't fire again for this flight:

- `day_before_fired` — T-24h sanity check
- `time_to_leave_fired` — Traffic-aware leave-by alert
- `boarding_fired` — Status transition to `boarding`
- `arrival_logistics_fired` — T-arr−15min logistics push
- `landed_acknowledged` — User acknowledged the landing notification
- `connection_at_risk_fired` — Cross-flight: projected transfer window on this leg-2 has fallen below `min_transfer_minutes`. Carried on the leg-2 (downstream) record so the marker survives leg-1 landing
- `gate_assignment_fired` — The once-per-flight gate + terminal readout has fired (first gate seen inside the pre-boarding window). `gate_change` is gated against this readout anchor: suppressed (recorded silently) until the readout fires; on the readout's own cycle only the redundant departure gate_change is dropped while an arrival-gate move still surfaces; and a flight already boarding or gone — whose readout never fires — surfaces gate moves rather than muting them forever

## Atomic Writes

Every `write_*` helper uses write-to-tmp + `os.replace` in the same directory so a kill mid-write doesn't leave a half-written file. Cross-device renames are not atomic — the state dir must live on a single filesystem.

## Migration Policy

Today `STATE_SCHEMA_VERSION` is `6`.

`state.py`'s owner-side read helpers (`read_config`, `read_active_flights`, `read_flight_state`) enforce these rules on `schema_version`:

- Equal to current → return the payload
- Higher than current → `StateError` (the owner is authoritative for its schema and must never run behind its own state files; never silently downgrade)
- Lower than current → run the owner-side migration in `_migrate`, rewrite at the current version, return the upgraded payload
- Missing, wrong type (non-int, including `bool`) → `StateError` with actionable repair message

Non-owner readers (sync-tripit, future cross-plugin composition) call the dedicated snapshot entry points: `read_active_flights_snapshot()` and `read_flight_state_snapshot(flight_id)`. These mirror the owner-side functions' return shapes but treat ANY `schema_version` other than `STATE_SCHEMA_VERSION` as "no usable prior state" (return `[]` / `None`) without invoking `_migrate` — lower means the owner hasn't migrated the file yet; higher means the reading plugin is lagging behind the owner mid-rollout and degrades safely instead of wedging until upgraded (per `coding-policy: stateful-artifacts`, Cross-Pipeline Schema Bumps). Corrupt JSON, a missing/non-int `schema_version`, or a missing required field at the current schema still raises `StateError` from the snapshot path. Only the owner skill (`flight-assist`, this plugin, via `state.py:_migrate`) migrates.

Migrations chain: `state.py:_migrate` steps a record through every intermediate version in one call (a v1 record runs v1→v2→v3 before returning), so an old file lands at the current version on its first owner-side read.

### v1 → v2

Per-flight state: `phase_markers` gains `connection_at_risk_fired: false`. The owner-side migration in `state.py:_migrate` adds the missing key on first read and rewrites the file at v2. Config and active-flights files have no shape change at v2 — they receive a schema_version bump only.

### v2 → v3

Per-flight state: gains the `calendar_events` map (empty `{}` on migration). The owner-side migration in `state.py:_migrate` adds the missing key on first read — scoped by the `flight-<id>.json` filename, not by payload contents, so a config/active-flights file (or any future record that happens to carry a `flight_id` key) is never given this per-flight-only field — and rewrites the file at v3. Config and active-flights files have no shape change at v3 — they receive a schema_version bump only.

### v3 → v4

Config: gains two optional calendar-reconcile fields, `byair_calendar_name` and `byair_calendar_id` (see `config.json` above). Both are optional and absent-tolerant, so there is no shape to add on migration — the owner-side `state.py:_migrate` only bumps the `schema_version`. Per-flight and active-flights files likewise have no shape change at v4 — schema_version bump only.

### v4 → v5

Config: gains five optional airport-clearance fields, `airport_clearance_domestic_minutes`, `airport_clearance_international_minutes`, `airport_post_arrival_domestic_minutes`, `airport_post_arrival_intl_us_minutes`, and `airport_post_arrival_intl_abroad_minutes` (see `config.json` above). All optional and absent-tolerant, so there is no shape to add on migration — the owner-side `state.py:_migrate` only bumps the `schema_version`. Per-flight and active-flights files likewise have no shape change at v5 — schema_version bump only.

### v5 → v6

Per-flight state: `phase_markers` gains `gate_assignment_fired: false`. The owner-side migration in `state.py:_migrate` adds the missing key on first read — scoped to the `flight-<id>.json` files — and rewrites the file at v6. Config and active-flights files have no shape change at v6 — they receive a schema_version bump only.

## Bump Procedure

When adding or renaming a field:

1. Bump `STATE_SCHEMA_VERSION` in `state.py` and document the new shape in this file
2. Add migration logic to `state.py` that reads old `schema_version` and rewrites the upgraded shape via the owner-skill code path (the precheck, the agent on wake, sync-tripit — every entry point that uses `read_flight_state` from inside `flight-assist`)
3. Non-owner reader skills (other plugins, future cross-plugin composition) call `read_active_flights_snapshot` / `read_flight_state_snapshot` instead of the owner-side helpers; the snapshot readers treat any mismatched `schema_version` as "no usable prior state" and return without rewriting. Migration happens exclusively on the owner-skill's next read
4. CHANGELOG entry describing the version bump and the owner-side migration path — an un-headed `### ` block at the top of `CHANGELOG.md`; the publish stamp step adds the `## <version> — <date>` heading (no `## Unreleased` section — that heading is forbidden per `coding-policy: context-artifacts` CHANGELOG Hygiene)

## Calendar-as-State: Airport Drive Blocks

A created airport drive block has no local record — the calendar event itself IS the state (Epic #59 §4, same design as drive-planner's meeting blocks but a self-contained sibling codec; see `#90`). The recheck poll re-fetches the near-term window by a direct API call and reads each of its own blocks back off the event. There is no `airport-blocks.json`. Owned by `airport_block.py` (`build_block_args` / `build_description` write, `parse_block` reads). This `BLOCK_SCHEMA_VERSION` is **distinct from** the on-disk `STATE_SCHEMA_VERSION` above — a separate, calendar-carried record with its own version line.

All state lives in the event **`description`** — the live Composio v3 calendar toolkit exposes NO writable `extendedProperties` (verified against the NAS during Phase 1), so the description is the only durable, writable surface. It carries three parts:

- the human line `Drive: → <CODE> (<flight>)` (to_airport) or `Drive: <CODE> → home` (from_airport);
- the self-marker `[flight-assist:flight=<id>:dir=<to_airport|from_airport>]` — recognizes flight-assist's own airport blocks for idempotent create; pinned against the codec's marker regex by a test;
- a `<!--fadrive:{...}-->` JSON comment (compact, hidden in most calendar UIs) with the machine state. The prefix is `fadrive`, **not** `fa`: flight-assist's `calendar_tags.py` already uses `<!--fa:{...}-->` for boarding/flight event tags, so the airport drive block carries its own prefix to avoid matching those:

| state key | meaning |
|-----------|---------|
| `schema_version` | record schema version (`BLOCK_SCHEMA_VERSION`, currently `1`) — spelled out per `coding-policy: stateful-artifacts`, which requires every record to carry an auditable `schema_version` field by that name; the remaining keys stay abbreviated for description compactness |
| `b` | routed drive seconds captured at creation (recheck baseline) |
| `a` | anchor timestamp, ISO-8601 — for `to_airport` the be-at-the-airport DEADLINE (`dep − clearance`); for `from_airport` the earliest the drive home can START (`actual_arr + post_arrival_delay`) |
| `o` / `d` | the routed leg endpoints (the poll re-routes exactly this pair) |
| `al` | comma-joined record of alerts already pushed — `growth` and/or `leave_now` — so a later poll never re-pings the same condition |

The served `flight_id` and leg `direction` come from the marker; the block's start/duration carry the times (CREATE uses flat `start_datetime` + `event_duration_*` plus the airport's IANA `timezone`).

Writer / reader contract:

- **Writer** — flight-assist creates blocks via the calendar-reconcile path (idempotent: finds an existing marker first, never double-books). When an alert fires, the recheck poll rebuilds the full `description` with only `al` updated and applies it via a partial `GOOGLECALENDAR_PATCH_EVENT` AFTER the send. `build_block_args` / `build_description` are the single source of the description format for both create and the suppression patch.
- **Reader** — the recheck poll calls `parse_block(event)`; a non-block or malformed event yields `None` (never raises), so one bad event can't abort the poll.

Migration (per `coding-policy: stateful-artifacts`):

- `schema_version` `1` is the initial version; no migration exists yet, so `parse_block` accepts **only the exact current version**. A record that is **missing** `schema_version`, carries a **non-int**, or differs from the current version in **either** direction — **older** or newer — all parse to `None` (no-usable-prior-state, the safe non-disruptive fallback). v1 is the first version with no pre-version legacy records, so accepting an older integer as "current" would silently trust an unmigrated shape — hence exact-match. When a future shape bumps the version, add the owner-side v1→vN upgrade in `parse_block` and widen acceptance to include the migratable older versions.

Tolerance:

- A block whose state is missing or malformed (no marker, unparseable JSON, unparseable baseline / anchor, empty endpoints, unknown direction, missing event id) parses to `None` and is treated as "not a block I recheck" — never raised on.
- Composio is mid-retirement (nanoclaw#638 → OneCLI workspace MCP); the API fetch / create / find / patch are the pieces that re-point later, same as the flight-assist reconcile path.
