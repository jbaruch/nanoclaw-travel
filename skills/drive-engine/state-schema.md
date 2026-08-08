# Drive-Engine State Schema

Documents the on-disk state files the drive-engine skill reads and writes. Per `coding-policy: stateful-artifacts`.

## Owner Skills

Each on-disk file has one owner module that owns its schema (only it migrates `schema_version`):

- `skip-state.json` — owned by `skip_state.py`. Writer and reader are co-bundled and go through the owner API: the skip action (`skip_drive.py`) writes via `add_skip`, the sweep (`reconcile_sweep.py`) reads via `load_active_skips`. No skill rewrites the file directly.
- `airport-facts.json` — owned by `airport_facts_cache.py`. The sweep (`reconcile_sweep.py`) both reads (`load_static_facts`) and writes (`store_static_facts`) through the owner API (#211).
- `drive-decisions.json` — owned by `drive_decision.py`. The sweep writes the drive-time-derived verdict (`record_drive_time`, `mark_asked`) and the answer action (`answer_drive_or_fly.py`) writes the operator's (`record_operator_answer`). `check-travel-bookings` is a read-only, non-migrating consumer in another skill (#231).

`skip_state.py` came from the retired `drive-planner` (#156), whose bundle was folded into drive-engine once drive-engine was its only importer (#181).

## State Directory

- Production: `/workspace/state/drive-planner/`
- Tests override via the `DRIVE_PLANNER_STATE_DIR` environment variable

All three files share this directory (`airport_facts_cache.py` and `drive_decision.py` reuse `skip_state.state_dir` as the single source of truth). The `drive-planner` name is deployed state, not a live reference — the store predates the #181 fold and renaming it would strand the skips already on disk. Rename only behind a migration.

## Files

### `skip-state.json`

The user's "skip this meeting" decisions, with per-skip expiry. Owned by `skip_state.py`.

```json
{
  "schema_version": 1,
  "skips": {
    "evt_42": "2026-07-01T17:00:00-05:00"
  }
}
```

Fields:

- `schema_version` (int, required) — currently `1`
- `skips` (object, required) — map of `meeting_id` → ISO-8601 expiry timestamp (tz-aware). The skip is active while its expiry is strictly after `now`; once expired it is dropped on the next read/prune and the meeting re-enters `needs_decision`.

Writer / reader contract:

- **Writer** — the skip-reply path, `skip_drive.py`. It resolves the meeting by summary, deletes its drive blocks, and calls `add_skip(meeting_id, expires=, now=)` with the expiry derived from the latest matched block anchor (meeting end) plus a pad, so the skip lapses once the meeting is past. `clear_skip(meeting_id, now=)` undoes a skip; `prune(now)` reclaims disk. All three go through the owner (`skip_state.py`) API.
- **Reader** — the sweep (`reconcile_sweep.py`) calls `load_active_skips(now)` and passes the result to `scan(skip_state=...)`. `scan.py` consumes the returned `{meeting_id: expiry}` mapping; it never touches the file.

Tolerance:

- A **missing** file is not an error — it is indistinguishable from "no skips yet" and reads as an empty map.
- A **present but corrupt** file (unparseable JSON, non-object root, missing/invalid `schema_version`, or a `schema_version` below the current floor) raises `SkipStateError` rather than being silently treated as "no skips" — silently resetting would resurrect every skipped meeting as a nag.
- A `schema_version` **newer** than this plugin is **refused** with `SkipStateError` on **both** paths — read (`load_active_skips`) and write (`add_skip` / `clear_skip` / `prune`). The fix is to upgrade the plugin to accept the new version.
  - The **read** path fails closed (#184) rather than taking `stateful-artifacts`' no-prior-state branch. An empty skip map is not inert: it drops every active skip, so the sweep re-plans each meeting the operator declined and pings them about it — the "escalates work" a no-prior-state fallback is forbidden to become, and precisely the lombot #49 nag this file exists to prevent. Raising surfaces at `reconcile_sweep.main`'s fail-closed boundary as a clean no-wake skip: the same whole-cycle skip any sweep error takes. No partial plan, no nag. The cost is explicit — while the file is future-versioned, no drive blocks are planned at all.
  - The **write** path additionally must not proceed because it would rewrite the future-version file as v1 and clobber a newer writer's state.
  - Reachable only via a plugin **downgrade** after a future v2 ships, or a hand-edited file: writer and reader co-ship in one bundle in one plugin, published together, so there is no cross-pipeline skew window (`coding-policy: stateful-artifacts`, Cross-Pipeline Schema Bumps).
- Malformed individual entries (non-string id or expiry, unparseable/naive expiry) are dropped, not fatal.

Migration:

- `schema_version` `1` is the initial version; no migration exists yet. A future shape change bumps the version and adds the owner-side upgrade-on-read per `coding-policy: stateful-artifacts`. A version below the current floor has no migration path (v1 is first) and is refused; a version above is refused on both paths until the plugin is upgraded to accept it (see Tolerance — this artifact fails closed rather than reading a newer file as no-usable-prior-state, #184).

### `airport-facts.json`

The cross-sweep cache of **static** airport facts — IATA code, country flag, IANA timezone — keyed by byAir `airport_id`. Owned by `airport_facts_cache.py`. Introduced in #211 to stop the sweep re-fetching immutable facts from byAir every ~30-min cycle (~7.6s at 13 airports, the dominant plan-phase cost that froze the calendar).

```json
{
  "schema_version": 1,
  "airports": {
    "3": {"iata": "JFK", "flag": "🇺🇸", "tz": "America/New_York"}
  }
}
```

Fields:

- `schema_version` (int, required) — currently `1`
- `airports` (object, required) — map of `airport_id` (string key) → `{iata, flag, tz}`. `iata` is always a non-empty string (a None-IATA resolution is a transient miss, never cached); `flag` / `tz` may be `null`.

Writer / reader contract:

- **Writer / Reader** — the sweep (`reconcile_sweep.py`) both reads (`load_static_facts`) and writes (`store_static_facts`) through the owner API. It writes only when a sweep learned a fresh fact (a first-seen airport, or a changed one). No other skill touches the file.
- byAir's live `delay.index` congestion nudge is **not** cached here — it changes through the day, so the sweep fetches it live and only for near-term departures (`reconcile_sweep._near_term_departure_airport_ids`).

Tolerance — **hint, not authority** (the deliberate opposite of `skip-state.json`):

- A **missing**, **unreadable/corrupt**, **non-object**, or **future-versioned** file all resolve to an **empty map** — the sweep re-fetches from byAir this cycle, exactly the pre-cache behaviour. Nothing raises; a diagnostic goes to stderr for anything but a plain missing file. A stale-vs-fresh static fact costs only latency, never a wrong block, so failing closed (as the skip store does) would be the wrong trade.
- Malformed individual entries (missing/empty `iata`, non-object value, non-integer key) are dropped; a well-formed remainder is still returned.

Migration: `schema_version` `1` is the initial version. Because a future version reads as no-usable-prior-state (refetch, non-disruptive), a newer writer's file survives untouched until this reader is upgraded — no fail-closed refusal is needed (`coding-policy: stateful-artifacts`, Cross-Pipeline Schema Bumps). The refetch is non-disruptive even when byAir is also down: a cache-miss airport that byAir can't resolve makes `reconcile_sweep._resolve_one_airport` raise `AirportUnresolved`, failing the whole sweep closed rather than building a partial plan that would orphan-delete live blocks (#211). So the empty-map fallback never escalates work — it costs at most one slow (or skipped) sweep, never a wrong or deleted block.

### `drive-decisions.json`

Whether each flight-less trip is a drive or an unbooked flight, with per-trip expiry. Owned by `drive_decision.py`. Introduced in #231, when a trip booked with lodging and no flight got neither a getting-there drive nor a missing-flight warning.

```json
{
  "schema_version": 1,
  "trips": {
    "tn-tigers-vs-faith-christian-school-2026-08": {
      "verdict": "drive",
      "decided_by": "operator",
      "drive_seconds": 13200,
      "asked_at": "2026-08-08T18:00:00+00:00",
      "expires": "2026-08-17T15:00:00+00:00"
    }
  }
}
```

Fields:

- `schema_version` (int, required) — currently `1`
- `trips` (object, required) — map of trip key → record. The key is `travel-core`'s `trip_key(summary, start)`, the same slug `travel-db.json` buckets a trip under, so the booking-gap consumer joins on it without a second identifier.
- `verdict` (string, required) — `drive`, `fly`, or `unknown` (the drive time is ambiguous and the operator has not answered)
- `decided_by` (string, required) — `drive_time` or `operator`
- `drive_seconds` (int or null) — the routed home→lodging drive the verdict was read from
- `asked_at` (ISO-8601 tz-aware string or null) — when the drive-or-fly question went out; null while unasked
- `expires` (ISO-8601 tz-aware string, required) — the verdict applies while this is strictly after `now`

The band thresholds and the verdict each maps to live in `lodging_source.py` (`DRIVE_CERTAIN_MAX`, `DRIVE_IMPLAUSIBLE_MIN`, `classify_drive`); per `coding-policy: script-as-black-box` they are not restated here.

Writer / reader contract:

- **Writers** — both co-bundled, both through the owner API. The sweep (`reconcile_sweep._plan_lodging_legs`) calls `record_drive_time` each cycle and `mark_asked` when it hands the question to the payload. The answer action (`answer_drive_or_fly.py`) calls `record_operator_answer`.
- **Precedence** — `record_drive_time` never overwrites an unexpired verdict whose `decided_by` is `operator`, so a sweep landing after the answer cannot revert it and re-ask. `asked_at` likewise survives a re-derivation, so the question is asked once per trip, not once per sweep.
- **Reader (same skill)** — the sweep calls `load_verdicts(now)` and passes the result to `lodging_source.lodging_desired_blocks`.
- **Reader (other skill)** — `check-travel-bookings` reads the file directly, read-only and non-migrating, to report a missing flight for a `fly` verdict. It never writes and never upgrades a version.

Tolerance:

- A **missing** file is not an error — indistinguishable from "no verdicts yet", reads as an empty map.
- A **present but corrupt** file (unparseable, non-object root, missing/invalid `schema_version`, `trips` not an object, or a version below the current floor) raises `DriveDecisionError`. Reading it as "no verdicts" would drop every recorded answer and re-ask about every settled trip — the nag `skip-state.json` fails closed for the same reason.
- A `schema_version` **newer** than this plugin is **refused** on both read and write, mirroring `skip-state.json`: reading it as empty loses answers, and a write would rewrite a newer writer's file as v1.
- Malformed **individual** records (bad verdict, bad decider, missing/unparseable expiry) are dropped rather than fatal — unlike a corrupt file, one bad record only means that trip has no verdict, and the sweep re-derives it from the drive time this cycle.
- The **cross-skill reader** in `check-travel-bookings` is deliberately looser: a missing, unreadable, or unrecognized-version file yields no verdicts and therefore no gap. Its no-prior-state path must stay non-disruptive (`coding-policy: stateful-artifacts`), and inventing a missing-flight alert from an unreadable file is exactly the alert storm that forbids.

Migration: `schema_version` `1` is the initial version. Writer and cross-skill reader ship in one plugin published together, so there is no skew window; a future bump adds the owner-side upgrade-on-read in `drive_decision.py` and widens the `check-travel-bookings` reader's accepted set first, per `coding-policy: stateful-artifacts` Cross-Pipeline Schema Bumps.

## Calendar-as-State: Drive Blocks

A drive block has no local record — the calendar event itself IS the state (Epic #59 §4). The sweep re-fetches the near-term window by a direct API call and reads each block back off the event. There is no `blocks.json`; the local state files are `skip-state.json`, `airport-facts.json`, and `drive-decisions.json` above.

Every block the engine writes is owned by `block_codec.py` — marker template, machine-state keys, the generations it recognizes, and its version/tolerance rules all live there as named constants and its module docstring. Per `coding-policy: script-as-black-box`, this file does not restate them.

### Where the state lives — the #178 migration (dual-read → writer flip)

Machine state has moved off the human-visible event **description** into **`extendedProperties.private`**, a machine-only field. The description carried it only because the Composio v3 toolkit exposed no writable `extendedProperties`; the native Calendar API (nanoclaw#638) does, so the constraint is gone. Blocks deployed before the flip still carry their state in the description, so the move is a live-data migration with a transition window, not a field swap.

Rollout order (per `coding-policy: stateful-artifacts`, Cross-Pipeline Schema Bumps — dual-accept readers ship before the writer flips):

1. **Dual-read (done).** `block_codec.parse_block` reads `extendedProperties.private` FIRST and the description SECOND — a block written either way round-trips. `fetch_events` carries `extendedProperties` through its field projection so the reader receives it. Shipped as its own release and materialized in production before the writer flip, so no container ever runs the new writer against an old reader.
2. **Writer flip (done).** `calendar_apply` writes `build_extended_properties` on create and patch, and the `description` now carries only the operator-facing route line (`origin → destination`) — the marker + `<!--dengine:-->` comment no longer squat there. No recognizer needed changing: `meeting_source.exclude_drive_block_events` already recognizes a block through `parse_block` (so it inherited dual-read), and `scan.py`'s marker is the unrelated legacy `drive-planner` one. A block still carrying description-state from before the flip is read by the fallback and migrated to `extendedProperties` on its first post-flip shift (`build_patch_args` replaces the description); one that never shifts ages out of the near-term window.
3. **Drop the description reader (follow-up).** Once no description-state block remains in the near-term window, retire the description branch of `parse_block` and the legacy-generation readers that only exist for description-carried state.

Extended-properties shape (`extendedProperties.private`, a flat string→string map; `block_codec.build_extended_properties` is the source of truth): every key is `dengine_`-namespaced to stay collision-safe in the shared map, every value is a string. `dengine_schema_version` (auditable version, spelled out per `coding-policy: stateful-artifacts`), `dengine_leg` (leg identity), `dengine_kind`, `dengine_b` (baseline seconds), `dengine_a` (anchor ISO-8601), `dengine_we` (transfer window end, optional), `dengine_o` / `dengine_d` (routed endpoints), `dengine_al` (comma-joined alert record). A map whose version is missing or not the current one reads as "no unified state here" and the reader falls back to the description, mirroring the description reader's unknown-version handling. `UNIFIED_BLOCK_SCHEMA_VERSION` (the drive-engine codec's version constant) is unchanged — the state's fields and meaning are identical, only its carrier moves.

Blocks are stamped Tangerine (`colorId` "6") so they read as visually distinct from meetings and flights (#167). The colour is a write-only presentation attribute, not machine state read back off the event — `calendar_apply.py` sets it on both create and shift (named constant `_DRIVE_BLOCK_COLOR_ID`); no reader consults it.

The API fetch / create / patch / delete go through `google_calendar_client` — the native Calendar REST API, brokered by OneCLI's gateway (nanoclaw#638).

### Legacy drive-planner blocks — recognized, never written

Blocks the retired drive-planner (#156) left on the calendar carry a `[drive-planner:meeting=<id>:dir=<dir>]` marker and a `<!--dp:{...}-->` state comment. **Nothing writes this shape** — the sweep that did is retired and its codec is deleted (#181). Two readers still care, and both read the marker only:

- `scan.py` (`_MARKER_RE`) buckets the served meeting as `has_block`, so the engine does not plan a duplicate drive on top of a block that already exists;
- `block_codec.parse_block` classifies the event as `GEN_LEGACY_DP` on the marker plus the *presence* of the `<!--dp:-->` comment, so `meeting_source.exclude_drive_block_events` can keep it in the scan input while dropping the engine's own blocks.

The `<!--dp:-->` payload's keys (`v`, `b`, `a`, `o`, `d`, `al`) are **not decoded by anything** — `block_props.parse_block` was their only reader and went with #181. They are inert bytes on deployed events; the comment survives as a recognition signal, not a record.

The engine never converges or deletes these blocks (`_MANAGED_LEGACY` is empty) — the operator cleans them up. Once none remain on the calendar, both readers above are dead code and can go.
