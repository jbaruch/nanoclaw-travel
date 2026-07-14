# Changelog

### Fixed — drive-engine routing storm hung the sweep (#172)

The airport-endpoint fix in #165 (`"STN airport"` instead of a bare `"STN"`) made
airport legs geocode *successfully* — but many airports return Google Distance
Matrix `ZERO_RESULTS` and fall back to TomTom's three-call geocode+geocode+route
chain at up to 10s each. Uncached and unbounded, a multi-leg itinerary routed for
minutes on every sweep (observed live: 80+ `maps.googleapis.com` + 30+ TomTom calls
in 15 minutes), and the agent container that ran it was SIGKILLed at its timeout
with no output. Two fixes:

- **Per-sweep route memoization.** `reconcile_sweep.make_route` wraps the maps
  client in a `(origin, destination)` cache shared by every airport and meeting
  leg, so an endpoint that appears in several legs (a departure destination that is
  also a transfer origin) is routed once per sweep, not per leg — the caller-level
  caching `MapsClient`'s own docstring prescribes. A failed route caches `None` so a
  dead endpoint isn't re-attempted (and re-failed-over) every leg.
- **Plan-phase wall-clock budget, enforced per route call.** `build_reconcile_plan`
  polls an injected `has_budget` once per leg, and `make_route` itself refuses to
  START a new route call once the deadline passes — both raise `PlanBudgetExceeded`.
  Enforcing at the route call (not only between legs) means a single leg's provider
  fallback chain can't push the sweep past its budget after the per-leg poll already
  passed. The sweep catches the exception and skips the cycle cleanly
  (`wake_agent: false`) rather than being killed mid-route before it can print JSON.
  The abort is deliberately all-or-nothing: a partial `desired` set would read as
  orphaned blocks and get deleted, then recreated next sweep — the exact churn #164
  fixed. The next sweep resumes; the reconcile is idempotent.
- **Tightened per-call timeout for the sweep's maps client** (4s, from the shared
  10s default) so a single `travel_time` — one Google call plus up to three
  sequential TomTom fallback calls — finishes within the margin between the plan
  budget and the host's ~33s precheck kill, guaranteeing the clean no-wake payload
  is emitted first.

## 0.2.43 — 2026-07-13

### Fixed — drive-engine airport legs never produced ('origin route failed') (#165)

Every airport drive leg failed routing while meeting legs built fine. Two causes:

- **Past/completed segments weren't filtered.** The engine built airport legs for
  *every* flight in the schedule, including a trip that already flew — so it tried to
  route the operator's current home (Tennessee) to a departure airport abroad that flew
  last week (London Stansted). There's no driving route across the Atlantic, so Maps
  returned nothing and the leg "failed." The pipeline now skips any leg whose drive
  window is already in the past (`anchor`/`window_end` < now) before routing it — a
  completed trip correctly produces no drives, cleanly, with a `past, skipped` diagnostic
  instead of a route-failure.
- **Airport endpoints were bare IATA codes.** Airport legs fed the maps route a bare
  3-letter code (`"STN"`), which Distance Matrix can't reliably geocode (meeting legs
  route because they carry full addresses). They now use the geocodable form the
  `MapsClient` documents (`"STN airport"`), so future flights route reliably instead of
  the airport half being silently dead while meeting drives work.

## 0.2.42 — 2026-07-13

### Fixed — drive-engine duplicate storm + apply timeout (#164)

Every ~30-min sweep was killed mid-write past the host's ~33s precheck budget and left
a fresh set of duplicate drive blocks — the calendar accumulated 10+ copies of each leg.
Two root causes, both fixed:

- **Update churn (the storm).** The maps route returns a slightly different
  `baseline_seconds` on every sweep (traffic-recomputation jitter — 1–46s swings for an
  unchanged leg). `_needs_update` compared it exactly, so every sweep re-"shifted" all
  ~15 legs, and each shift was a **recreate-then-delete**: the ~33s kill landed after the
  recreates but before the deletes, duplicating every leg every cycle. Fix: (1) an update
  now only fires on a **meaningful** baseline change (≥ 120s; sub-2-min jitter is ignored),
  and (2) a shift is a single **in-place `PATCH_EVENT`** of the same event — no second
  event is ever created, so a kill can no longer leave a duplicate. Verified live that a
  PATCH shifts a block's start + description to the correct instant without a duplicate.
- **Unbounded apply.** `apply_plan` now takes a wall-clock **budget** (the sweep gives it
  whatever of a 27s budget the fetch/plan phase left) and stops starting new write ops
  once it elapses — deletes first (so a duplicate backlog drains), then creates/converts/
  updates — returning a clean payload with a `deferred` count instead of being killed
  mid-write. Deferred ops drain on the next sweep; the reconcile is idempotent, so
  resuming never duplicates. The existing duplicate backlog is cleaned up by the engine's
  own G1/G7 delete path over the next few sweeps.

## 0.2.41 — 2026-07-13

### Added — flight-assist trip-window defense-in-depth (#147)

The host now owns the primary control: a pre-spawn gate (jbaruch/nanoclaw#754) that
reads the group `travel-db.json` and does not spawn the flight-assist container outside
a trip window, so the `*/2` cadence costs nothing off-trip. This adds the plugin-side
belt-and-suspenders: `trip_window.evaluate_trip_window` reads the **same** file with the
**same** window (`(start − 24h) ≤ now < (end + 24h)`, union of trips) and the **same**
asymmetric fail semantics (absent → out of window; corrupt / unreadable → fail open so a
bad file never blinds an active trip). The precheck consults it first and, if a container
was spawned off-window anyway, exits before any byAir call with
`{"wake_agent": false, "data": {"reason": "outside_trip_window"}}`.

`travel-db.json` (owned by `check-travel-bookings`, written nightly) is the single source
of truth for active trips — no second trip store. As a cross-plugin non-owner reader,
`trip_window` gates on `schema_version` (`coding-policy: stateful-artifacts`): a version
other than the accepted `1` is no-usable-state and **fails open**, so a cross-pipeline
schema bump defers to the host gate instead of blinding a trip. The trip-window gate is
trip-level and stacks with the existing flight-level `_POLL_HORIZON_HOURS = 24`. Documented
in `skills/flight-assist/state-schema.md` and the owner's reader contract in
`skills/check-travel-bookings/state-schema.md`; `FLIGHT_ASSIST_TRAVEL_DB` overrides the
path for tests.

## 0.2.40 — 2026-07-13

### Fixed — drive blocks show as accepted, not an unconfirmed invite (#158)

Drive blocks were created with a `needsAction` self-attendee, so they rendered as
pending invites the operator had to RSVP to. The unified engine's create args now pass
`exclude_organizer: true`, which stops Composio from injecting the connected user as an
attendee — the block has no attendees and shows as a plain accepted event. Verified
against the live Composio toolkit (create with `exclude_organizer` → zero attendees).

The companion ask — a distinct calendar colour (Tangerine / `colorId: "6"`) — is deferred:
no Composio Google Calendar action (create / update / patch / quick-add) exposes an event
colour field in the deployed toolkit, so it cannot be set through the current write path.
Tracked separately, blocked on the Composio retirement / workspace-MCP migration.

## 0.2.39 — 2026-07-13

### Fixed — flight-assist day-before notification: wrong flight code + hallucinated airport (#159)

Two independent defects in the day-before notification, both trust-eroding though the
underlying tracking was correct:

- **Bug 1 — operating designator shown instead of marketing.** For a codeshare, byAir's
  two endpoints describe the same flight from opposite sides: `list_trips` (what
  `sync_tripit` seeds from) carries the marketing code `DL4908` with `operator.code` =
  `9E4908`, while `get_flight` (the poll) carries the operating code `9E4908` at top level
  and exposes the marketing code only in free-text `note`. The precheck poll was
  overwriting the sync-seeded marketing code with `get_flight`'s operating designator every
  cycle. `code` is now preserved across polls as a seed-time identity field (alongside
  scheduled times / airport ids), and the persisted snapshot is realigned to it, so no
  reader can surface the operating code. No structured marketing field exists in
  `get_flight` to read, so preservation — not re-extraction — is the fix. Because
  preservation would also keep an already-corrupted value, `sync_tripit` now repairs the
  `code` on every retained flight from `list_trips` (the marketing-code authority) each
  daily run, healing records poisoned by pre-fix polls.
- **Bug 2 — arrival airport free-typed.** State carried only numeric `dep_airport_id` /
  `arr_airport_id`, so the LLM composer invented "Stansted" (the trip's origin) for a
  JFK→Nashville arrival. The poll now captures the resolved airport `code` + `name` off the
  byAir payload it already fetches (`depAirport` / `arrAirport`) into `last_snapshot`, and
  the compose step renders the airport strictly from those fields — never free-typing from
  an id.

State schema bumped **v6 → v7** for the new `last_snapshot` airport slice and the realigned
`code` semantics. The airport fields are byair-owned and repopulated by the next poll, so the
owner-side `state.py:_migrate` v6→v7 step is a schema_version bump only (no backfill).

## 0.2.38 — 2026-07-13

### Changed — wire the drive-engine's remaining designed features live (#156)

Three refinements the earlier live cutover built but left unwired are now active in
`reconcile_sweep.py`:

- **R2 byAir ∪ TripIt union** — the sweep now also parses the travel-schedule's
  `Flight` segments (new `tripit_flights.py`, a bounded `<DEP> to <ARR>` iCal parse)
  and unions them with the byAir records, so a flight tracked by only one source
  still produces legs. `build_plan` gains `tripit_flights`.
- **R5 identity flight-mask** — flight events are now dropped from the meeting scan
  input by `flight_mask.is_flight_event` (identity only, never time overlap, so a
  ground meeting overlapping a redeye window survives), and `scan` runs with an
  empty flight context.
- **V3 boarding-block presence** — trivial-leg suppression is gated on a real
  boarding block on the byAir calendar; absent one, the trivial airport drive is
  kept (R6 — never silently drop the only "head to the gate" signal). `build_plan`
  gains `boarding_present`.

## 0.2.37 — 2026-07-13

### Changed — drive-engine goes live; drive-planner and flight-assist airport drives retired (#156)

The unified drive-engine now WRITES: `reconcile_sweep.py` plans airport drives from the byAir itinerary and meeting drives from the calendar (reusing drive-planner's proven `scan` for meeting detection), diffs both against the primary calendar in one reconcile, and applies the result — creating / updating / deleting its own unified-codec blocks. Meeting drives gain travel-awareness (a drive whose routed time is implausible — the operator is abroad while the meeting is at home — is suppressed, not invented) and render in the meeting's local timezone. The engine touches ONLY its own blocks (`managed_legacy` empty): existing drive-planner blocks are left for the operator to remove.

The two legacy engines are retired: flight-assist's `scripts/reconcile.py` no longer runs the airport-drive pass (a dormant `airport_drive: {"status":"retired"}` marker remains), and `drive-planner` / `drive-planner-recheck` lose their cadences and become non-invocable library skills (their `scan` / `fetch_events` / `skip_state` modules are imported by the engine). New modules: `calendar_apply` (the write path, atomic convert/update with rollback), `meeting_source` (meeting legs + travel-away suppression). `DesiredBlock` gains a `timezone` field for local-time rendering.

## 0.2.36 — 2026-07-13

### Added — travel-core shared library bundle (#156)

Extracted `trip_origin.py` (TripIt-over-home position/anchor resolution) and `airport_lead.py` (airport clearance / post-arrival buffer policy) out of `flight-assist` into a new `travel-core` skill bundle, so flight-assist, drive-planner, and the incoming unified drive engine import one source of truth instead of reaching cross-bundle into flight-assist for shared logic. `travel-core` is a background library skill (`user-invocable: false`, `disable-model-invocation: true`) — no workflow, just hosted modules the consumers put on `sys.path` via the runtime-mount / dev-sibling pattern already used for `maps_client`. Consumers (flight-assist `precheck` / `airport_drive_reconcile` / `airport_drive_inputs`, drive-planner `precheck`) and the pyright execution-environment config were repointed accordingly; behavior is unchanged.

### Added — unified drive engine foundations (#156)

First two pure, deterministic modules of the leg-based drive engine that will replace the flight-assist / drive-planner two-engine patchwork. `flight_identity.py` unions the byAir and TripIt flight sources on a canonical identity of `(dep_airport, arr_airport, scheduled_dep_instant ± tolerance)` that excludes the designator — so a single physical flight tracked under two byAir ids / codeshare codes (FR7382 / MW7382) collapses to one flight instead of storming the calendar with duplicate drive blocks. `chain.py` classifies each consecutive flight pair (overnight / different-airport transfer / same-airport connection) and plans the ground legs a trip yields: same-airport connections default to airside silence and interior connections are suppressed, so a drive is never routed to an airport the operator reaches by a prior flight.

### Added — drive-engine skill in read-only preview mode (#156)

The unified engine now ships as the `drive-engine` skill, running in read-only preview (shadow) mode on a ~30-min precheck. It assembles the airport drive legs the byAir itinerary needs (`position_at` origins, §B anchors, GPS-imminence overlay, connection suppression, trivial-leg suppression), diffs them against the primary calendar's current drive blocks (recognizing the new codec and both legacy fadrive/dp shapes), and logs the add/move/delete/replace plan — storm dedup, legacy convergence, orphan deletion — WITHOUT touching the calendar. `wake_agent` is always false; the precheck fails closed to a no-wake payload on error. This is the validation harness to confirm the plan against the live calendar before the write path is enabled; the two legacy engines stay in place until it is validated.

## 0.2.35 — 2026-07-09

### Fixed — drive-planner: catch flight events by content, not only by schedule time (#85 follow-up)

The 0.2.32 flight filter matched a calendar event to a scheduled flight by time overlap alone, and that was defeated by garbage in the calendar. Google "events from Gmail" auto-created three copies of one flight ("Flight to Nashville (DL 4908)"); two carried a corrupted timezone (span 19:55–22:01Z) that ended before the true flight window (22:59–01:46Z) began, so they missed the overlap, slipped through as ordinary New-York meetings, and drew a Stansted→New-York transatlantic "drive" (`ALL_PROVIDERS_FAILED`). The filter wasn't broken — the time-only match was too narrow for duplicate, time-corrupted input.

`scan()` now treats an event as air travel by **any** of three independent signals: (1) time overlap with a scheduled flight window (the 0.2.32 behavior); (2) a flight-template summary — `Flight to …` or a `✈` prefix — which is intrinsic to `scan` and needs no schedule, so it catches the corrupted duplicates; (3) an IATA flight designator (e.g. `DL 4908`) in the summary matching a scheduled flight's identity (new `trip_origin.flight_summaries()`), which survives a corrupted time. The precheck loads windows and summaries in one schedule read (`_build_flight_context`). The template signal is deliberately narrow (`Flight to `/`✈` prefix) so a real ground meeting is not silently withheld; a Gmail restaurant reservation ("Reservation at Fletchers House") stays a valid drive target. The filtered reason generalizes to "air travel — flight event".

## 0.2.32 — 2026-07-09

### Fixed — drive-planner: filter TripIt flight events out of ground-meeting classification (#85)

TripIt syncs each flight segment onto the primary calendar as its own event ("Flight to Nashville (DL 4908)", location = an airport). The sweep fetches every primary event and `scan.py` classified these flights as routable ground meetings, so the precheck tried to drive between airports — Stansted hotel → JFK for a layover, an ocean apart. Ocean routes return `ZERO_RESULTS` and woke the agent with "Couldn't compute drive time"; layover bridges surfaced as "doesn't fit the gap" noise. This is the unbuilt half of #85: its downstream sanity gates (`MAX_REASONABLE_DRIVE_SECONDS`, bridge>gap) catch implausible *drives* but never stopped a flight *event* from being treated as a meeting; the #122 anchor fix handled the restaurant-origin case but not this one.

`scan()` now takes `flight_windows` — the UTC spans of the TripIt schedule's `Flight` segments (new `trip_origin.flight_windows()`, read from the same `travel-schedule.json` the #122 anchor resolver already loads). Any event overlapping a flight window is bucketed `filtered` ("air travel — TripIt flight segment") and excluded as a routing neighbour, so a flight's airport location never draws a cross-continent drive and never bridges an adjacent real meeting. Matching is interval overlap (the calendar event and its schedule twin derive from the same TripIt data); only timed flight segments produce a window, so a date-only artifact can't suppress a real same-day meeting. Empty windows (no schedule, or the flight-unaware `scan.py` CLI) preserve the pre-#85 behavior — a real meeting is never suppressed. Also fixed a pre-existing duplicate `# 4.` step number in `_classify`.

## 0.2.30 — 2026-07-08

### Changed — consolidate dependency automation on Renovate; retire Dependabot

Both scanners ran after Renovate onboarded (#109), so every upstream release arrived as two PRs and each merge published a version — the github/gh-aw-actions patch stream alone shipped two published bumps in one day (0.82.3 via Dependabot #138, 0.82.4 via Renovate #141) plus a third still-open PR for 0.82.6 (Renovate #145). Removed `.github/dependabot.yml`; Renovate's `config:recommended` already covers both managers Dependabot tracked — `pip` (`requirements-dev.txt`) and `github-actions` — so no coverage is lost. Ported Dependabot's `dependencies` label to `renovate.json` to preserve PR filtering. GitHub-native Dependabot **security** alerts are a repo setting, not this file, and are unaffected.

## 0.2.29 — 2026-07-08

### Changed — renovate: stop proposing CI Python bumps past the container runtime

Renovate's onboarding config treated the CI `python-version` pin as a dependency to chase upstream (its first sweep filed a 3.11 → 3.14 bump, closed unmerged). The pin exists to mirror the NanoClaw agent container's interpreter — `nanoclaw-agent` builds on `node:24-slim` (Debian bookworm), whose python3 is 3.11.2, verified against the live image. Testing on a newer interpreter than production executes would let 3.12+ syntax and stdlib usage pass CI and fail in the container's prechecks. A `packageRules` entry disables renovate's `python` dep updates; the pin moves manually when the container's base image does.

## 0.2.28 — 2026-07-08

### Changed — bump github/gh-aw-actions/setup to 0.82.4 (renovate, PR #141)

## 0.2.27 — 2026-07-08

### Changed — update jbaruch/coding-policy action digest to 759e589 (renovate, PR #140)

## 0.2.26 — 2026-07-08

### Added — Renovate onboarding config (`renovate.json`, PR #109)

## 0.2.25 — 2026-07-08

### Changed — bump github/gh-aw-actions/setup from 0.81.6 to 0.82.3 (dependabot, PR #138)

## 0.2.24 — 2026-07-08

### Changed — bump actions/cache/save from 5.0.5 to 6.1.0 (dependabot, PR #139)

## 0.2.23 — 2026-07-08

### Changed — bump actions/cache/restore from 5.0.5 to 6.1.0 (dependabot, PR #136)

## 0.2.22 — 2026-07-08

### Changed — bump ruff from 0.15.19 to 0.15.20 (dependabot, PR #111)

## 0.2.21 — 2026-07-08

### Changed — bump pyright from 1.1.408 to 1.1.411 (dependabot, PR #137)

## 0.2.20 — 2026-07-07

### Fixed — CREATE wall-clock expressed in the event's timezone arg (`jbaruch/nanoclaw-travel#131`)

Drive blocks landed ~6h early while traveling (live case 2026-07-07: the Fletchers House Rye outbound block sat at 09:15 BST for a 15:45 reservation). PR #87 passed an explicit venue `timezone` to `GOOGLECALENDAR_CREATE_EVENT` but left `start_datetime`'s wall-clock in whatever offset `leg_start` carried (the home −05:00); the Composio adapter ignores the offset and re-reads the wall-clock in the `timezone` arg, shifting the block by the home↔venue delta — invisible at home where the two zones agree. `build_block_args` (drive-planner `block_props.py` and flight-assist `airport_block.py`, same latent bug) now converts `leg_start` to the target zone via a `_wall_clock_in` helper before formatting; an absent or unresolvable `timezone` (raw offset strings — `_extract_timezone`'s `Etc/GMT±N` fallback resolves fine) leaves the datetime untouched, preserving prior behavior. Tests pin the Chicago→London 6h case, the `Etc/GMT±N` fallback, the unresolvable-tz guard, and the airport-block UTC→Chicago case. The deeper question of normalizing internal `arrive_by` to the venue tz at parse time stays open in #131.

## 0.2.19 — 2026-07-07

### Fixed — move drive-planner-recheck sibling imports inside the precheck JSON boundary (`jbaruch/nanoclaw-travel#126`)

`drive-planner-recheck/precheck.py` resolved and imported the co-shipped drive-planner bundle at module import time, before `main()`'s outer-boundary try block — a missing or mis-mounted sibling skill raised `FileNotFoundError` as a raw process crash instead of the scheduled-task JSON contract's `{"wake_agent": false, ...}` payload. The bundle is now resolved lazily via `_ensure_drive_planner_on_path()` (idempotent, same pattern as sync-tripit's `_load_flight_assist`), called from `main()`'s try block and from `evaluate_blocks`; the shared-module imports moved to function scope. A new test patches the resolver to raise and asserts the no-wake payload with exit 0.

## 0.2.18 — 2026-07-07

### Fixed — snapshot readers treat newer flight state as no usable prior state (`jbaruch/nanoclaw-travel#125`)

`state.py`'s non-owner snapshot readers (`read_active_flights_snapshot`, `read_flight_state_snapshot`) raised `StateError` on a `schema_version` above the module's own, contradicting `coding-policy: stateful-artifacts` (a reader seeing a newer record is lagging, not looking at broken state). During a cross-pipeline rollout where flight-assist bumps its schema first, `sync-tripit/precheck.py` — a non-owner reader — hit that `StateError` and emitted `wake_agent:false` with `precheck_internal_error` every poll until the consumer plugin upgraded. The `migrate=False` snapshot path now returns `[]` / `None` (no usable prior state) for newer versions, which degrades to the bounded mtime-based stale-state gate instead of wedging. Owner-side reads stay strict — the owner must never run behind its own state files. `state-schema.md` documents the split; the future-version snapshot tests are inverted to assert the fallback and that the file is never rewritten.

## 0.2.17 — 2026-07-07

### Fixed — harden build-travel-db against malformed travel-schedule input (`jbaruch/nanoclaw-travel#127`)

`build-travel-db.py` handled only a missing `travel-schedule.json`; a corrupt, non-UTF-8, partially-written, or wrong-root-shape file produced a raw traceback instead of the documented Step 4 failure surface. The schedule read now catches `OSError` / `UnicodeDecodeError` / `json.JSONDecodeError` and validates the root is a JSON array of event objects, exiting 1 with a stderr diagnostic that names the recovery path (re-run `refresh-travel-schedule.py`). Tests cover truncated JSON, non-UTF-8 bytes, object root, and array-of-non-objects.

## 0.2.16 — 2026-07-07

### Fixed — document Composio credentials in the README environment contract (`jbaruch/nanoclaw-travel#128`)

The README required-environment table listed only `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`, while `.env.example`, `check-env.py`, and the runtime clients also require `COMPOSIO_API_KEY` and `COMPOSIO_USER_ID` — a fresh install following the README got flight data working with calendar reconciliation and drive-block operations silently broken. The table now lists all four required credentials, a separate optional table covers `COMPOSIO_BASE_URL` and the `TOMTOM_API_KEY` routing fallback, and the `check-env.py` description names everything the script actually checks.

## 0.2.14 — 2026-07-07

### Fixed — trip-aware drive origins: lodging over static home while traveling (`jbaruch/nanoclaw-travel#122`)

The drive planners had no trip awareness: drive-planner routed every meeting leg from the static home (live case 2026-07-07: a UK dinner drew a "leave by 6:16 PM — 39-min drive" block computed from the Tennessee residence via a mis-geocoded venue), and flight-assist's origin ladder fell back to the same static home when the live-location snapshot was stale. New shared `skills/flight-assist/trip_origin.py` resolves the anchor from `travel-schedule.json` (TripIt truth): off-trip → home, unchanged; on an active Trip → the `location` of the latest Lodging event (check-in or check-out) within the trip span at or before the anchor time, which also resolves check-out→check-in gaps; before the trip's first lodging → the Trip's own location, else unresolved — home is never used mid-trip. drive-planner's scan resolves the anchor per meeting (`anchor_for`) so a 14-day sweep window can span on- and off-trip meetings; an unresolved anchor surfaces the leg as `unplannable` instead of routing it. flight-assist's time-to-leave origin and drive-home destination use the trip-aware effective home per cycle.

### Fixed — correct stale plugin-home claim in `home_address.py` docstring (`jbaruch/nanoclaw-travel#122` comment)

The docstring claimed drive-planner lives in `nanoclaw-trusted`; it lives in this plugin. The `trusted-memory` ownership references (which genuinely point at `nanoclaw-trusted`) are untouched.

## 0.2.13 — 2026-07-07

### Fixed — stop flagging elapsed nights as lodging gaps (`jbaruch/nanoclaw-travel#120`)

`classify_trip` in `check-travel-bookings.py` scanned trip nights from `trip_start` with no floor at today, so a trip already underway reported every un-booked past night as a gap (live case 2026-07-07: the Scotland trip surfaced 10 phantom past-night gaps that buried the correctly-matched current Airbnb). The night scan now starts at `max(trip_start, today)`; `today` is threaded in from `main()` as a parameter so the classifier stays pure and testable. Future-night gaps of underway trips and future-trip flags are unaffected.

## 0.2.11 — 2026-07-02

### Changed — backfill CHANGELOG entries for released versions 0.2.7–0.2.10

Versions 0.2.7–0.2.10 shipped without CHANGELOG entries. Every released version now has a heading; the entries are reconstructed from the merge commits that produced each release. No code change.

## 0.2.10 — 2026-07-02

### Added — wire pyright into CI as a zero-findings gate (`jbaruch/nanoclaw-travel#115`)

Add a `python -m pyright --warnings skills/ tests/` step in CI after ruff and before pytest (`--warnings` fails on warnings, not just errors), completing the diagnostics gate whose config landed in 0.2.8. The tree was already clean (0 findings); no source changes.

## 0.2.9 — 2026-07-02

### Changed — refresh coding-policy PR review workflows (`jbaruch/nanoclaw-travel#117`)

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.2.8 — 2026-07-01

### Added — pyright config and test-suite strictness (`jbaruch/nanoclaw-travel#116`)

Land `pyrightconfig.json` (per-bundle `executionEnvironments` for the skill-bundle `sys.path` layout) and bring `pyright skills/ tests/` to zero findings, tightening test-side typing. Pins `pyright` in `requirements-dev.txt`. The CI gate that enforces this lands in 0.2.10 (#115).

## 0.2.7 — 2026-07-01

### Changed — refresh coding-policy PR review workflows (`jbaruch/nanoclaw-travel#114`)

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.2.6 — 2026-07-01

### Changed — migrate manifest from legacy `tile.json` to `.tessl-plugin/plugin.json`

Ran `tessl plugin migrate`: the manifest moved to `.tessl-plugin/plugin.json`, `.tileignore` was renamed to `.tesslignore`, and the obsolete `tile.json` was removed. `tessl plugin lint` passes on a clean tree (the local-only, git-ignored `.mcp.json` is absent in CI). This unblocks #77 (drive-planner evals, which require the plugin-manifest form). Package-sense "tile" wording throughout the prose and docstrings is reconciled to "plugin" per `jbaruch/coding-policy: migrate-to-plugin`; NanoClaw config identifiers (`additionalTiles`), `v1/tiles/...` API routes, and the CI publish workflow (still `tessl tile lint`, which works via the alias — a separate CI-scoped change) are intentionally left as-is.

## 0.2.5 — 2026-07-01

### Fixed — correct owner tile for the `## Addresses` block: `nanoclaw-trusted`, not `nanoclaw-admin`

`home_address.py`'s docstring and its three `HomeAddressError` messages named `nanoclaw-admin` as the owner of the canonical `## Addresses` block. The owner is the `trusted-memory` skill in **`nanoclaw-trusted`** (`tessl__trusted-memory`), whose `state-schema.md` documents the block (schema v1) and names this tile's `home_address.py` as its reader. The block is populated and correct on the NAS; only the attribution was wrong, so the reader worked but its "block missing" errors would have sent the operator to the wrong tile. Origin of the error is Epic #59 §4/§7 (`nanoclaw-admin`), carried into the reader and a prior CHANGELOG entry; both corrected. The legitimate `nanoclaw-admin` references (the `composio-fetch` calendar-fetch precedent, `check-travel-bookings`/`nightly-travel-sync` migrations) are unaffected.

## 0.2.4 — 2026-06-30

### Changed — drive-planner sweep notification is script-built, id-free, skip-by-number (`jbaruch/nanoclaw-travel`)

`apply.py create` now returns a ready-to-send `message` string (`build_notification`) that the wake agent relays verbatim, instead of composing the notification itself. The Haiku cadence agent was improvising a raw calendar event id into the skip affordance ("Reply skip `<id>` if you're not driving") despite the skill forbidding it; deterministic message assembly removes the improvisation surface entirely. One created block ends with the plain line `Reply skip if you're not driving.`; several render a numbered list ending with `Reply skip 1, or skip 1 and N, to drop any.` (N an index that exists for the count) — so the operator skips by a bare word or a list number, never an id. Route-error / unplannable / failed lines and the silence rule are preserved in the script. The skip-reply handler now treats `skip` as the primary verb (numbered `skip 1` / `skip 1 and 3`), with `cancel` kept as a synonym.

## 0.2.3 — 2026-06-30

### Added — gate + terminal readout at the pre-boarding window; gate changes only after it (`jbaruch/nanoclaw-travel#103`)

All-day gate-assignment churn is replaced by a one-time departure gate + terminal readout, fired the first cycle a gate exists inside the pre-boarding window (`scheduled_dep − boarding_lead − 1h`), plus ordinary `gate_change` alerts only after that readout. Before the window, the latest gate is recorded to state silently and never notified — WN482's BNA gate changed four times across 2026-06-25 (D3 → D1 → C2 → D6), each a separate wake, none within an hour of boarding. The new `phase_markers.check_gate_assignment` marker carries dep gate + dep terminal (the navigation signal: which terminal to head to), defers to the first in-window gate appearance when assignment is late, and stays silent once the flight is already boarding/departed/cancelled/diverted (shared with #102's leave-by suppression via `_boarding_or_gone`); `precheck` resolves the boarding lead from the snapshot through `boarding_lead.py` and gates `gate_change` against the readout anchor: suppressed until the readout fires; on the readout's own cycle only the redundant departure gate_change is dropped (a simultaneous arrival-gate move still surfaces); and a flight already boarding or gone — whose readout never fires — surfaces gate moves rather than muting them forever. The lead reads the same snapshot fields as the calendar boarding block (`calendar_reconcile._resolve_lead`): today the widebody lead resolves only via the inbound-aircraft chain (`inbound.aircraft_model` → 50 min), and the narrowbody default (30 min) covers everything else; full top-level-model and transoceanic resolution arrives when the precheck stamps `aircraft_model` + airport coordinates into the snapshot (#55), at which point the window widens automatically with no change here. New event `gate_assignment` documented in SKILL.md Step 3 and `references/event-payloads.md`. `STATE_SCHEMA_VERSION` bumps 5→6 with an additive owner-side migration adding `gate_assignment_fired: false` to per-flight `phase_markers`.

## 0.2.2 — 2026-06-29

### Fixed — suppress `time_to_leave` once the flight is boarding or gone (`jbaruch/nanoclaw-travel#102`)

The traffic-aware leave-by gate (`phase_markers.check_time_to_leave`) no longer wakes the agent when the flight has already started boarding or departed. On a delayed flight or with a stale travel estimate, the leave-by moment can land after boarding begins, so the marker fired, the agent woke, found nothing useful to say, and stayed silent — 2 of 16 flight-assist wakes on 2026-06-25 were this wasted pattern. The gate now takes the current snapshot and returns silent when it reads real-boarding (via `wake_rules.is_real_boarding`, which screens out byAir's premature "boarding" label per #54) or a `departed`/`en_route`/`landed`/`cancelled`/`diverted` status. The `_is_real_boarding` predicate is promoted to the public `is_real_boarding` since it is now shared across `wake_rules` and `phase_markers`. The normal pre-boarding leave-by alert is unchanged.

### Changed — run the flight-assist cadence wake on Haiku (`jbaruch/nanoclaw-travel#101`)

The flight-assist `agentModel` moves from `claude-sonnet-4-6` to `claude-haiku-4-5-20251001`, joining sync-tripit, nightly-travel-sync, drive-planner, and drive-planner-recheck on Haiku. The wake-cycle work is deterministic-script output (`reconcile.py`) plus a fixed `reason → sentence` template lookup — the hard logic lives in scripts, not the LLM — so the cheaper model carries it. The interactive diagnose / set-home-base paths are user-triggered and unaffected.

### Added — wire airport drive blocks into the wake-cycle reconcile (`jbaruch/nanoclaw-travel#90`)

The airport drive blocks now run for real. `airport_drive_reconcile.run_airport_drive_pass(composio, now=)` is the wake-cycle entry point — it resolves the inputs from the environment and on-disk state (config `home_address`, the live drive origin, the byAir + Maps clients, the active flights' states) and runs `run_airport_drive_reconcile`; the `reconcile.py` script calls it after the byAir-calendar reconcile and folds the result into its JSON under `airport_drive`. The drive blocks live on the **primary** calendar, so the pass runs even when the byAir-flight reconcile returns `no_calendar`; it stays a dormant zero-op summary when routing is unavailable (no `GOOGLE_MAPS_API_KEY`, no `BYAIR_MCP_URL`, or no tracked flights), and a transient byAir/Maps/Composio failure during it is logged and recorded as `{"status": "error"}` without failing the rest of the cycle. The origin ladder (fresh `current-location.json` → `home_address` → None) is extracted to `state.resolve_live_origin`, the single resolver the precheck's time-to-leave query now delegates to as well, so the two paths can never disagree on where the user is. SKILL.md Step 3 documents the new `airport_drive` output object.

### Added — airport drive block orchestration: fetch, plan, execute (`jbaruch/nanoclaw-travel#90`)

`airport_drive_reconcile.py` gains `run_airport_drive_reconcile(states, composio=, byair=, maps=, origin=, home_address=, config=)` — the orchestration that drives the assembler end to end against the calendar. For each active flight it builds the warranted blocks, fetches the primary calendar once over the spanning window (reusing `calendar_reconcile`'s live-verified `_find_events_args` / `_items`), and per block runs `plan_drive_block` and executes the create / shift via Composio. Calendar-as-state, no ledger: an existing block's no-op `signature` is derived from the block's OWN stored `anchor` + baseline (round-trip-stable, byte-identical to `DesiredDriveBlock.signature()`'s arithmetic) rather than from Google's start/end echo, so the offset-format ambiguity that bit #83 can't cause spurious shifts. A shift is a recreate-then-delete — create the replacement first so a transient create failure never leaves a gap, then delete the old, rolling the new one back if that delete fails so a cycle never leaves a duplicate — and every write goes through `build_block_args`' timezone-aware create path; a re-routed leave-by that drifts under `_REANCHOR_THRESHOLD` (5 min) from the block already on the calendar is suppressed, so traffic jitter doesn't rewrite the event every poll (#90 §7). The fetch window is anchored on each flight's stable scheduled times (not only the delayed desired window), so a block created before a delay is still found and shifted rather than duplicated however far the flight has moved. Per-op create/delete failures are collected, not raised — one bad write defers that op to the next cycle; a one-shot calendar fetch failure propagates (matching `calendar_reconcile`). Not yet wired into the wake-cycle `reconcile.py` (the client construction + origin resolution land in the follow-up PR on #90).

### Added — airport drive block assembler, the reconcile/route half (`jbaruch/nanoclaw-travel#90`)

flight-assist gains `airport_drive_reconcile.py` — the I/O-bearing layer that turns a flight's persisted state into the `DesiredDriveBlock`s the planner reconciles. `build_drive_blocks_for_flight(state, byair=, maps=, origin=, home_address=, config=)` gates on the flight's `computed_status` (a `to_airport` block while it hasn't left — scheduled/check-in/boarding; a `from_airport` block once airborne or down — departed/en_route/landed), resolves each direction's airport context via `byair.get_airport` (flag/`delay.index`/IANA-tz/code through `airport_drive_inputs.airport_context`), routes the leg via `maps.travel_time` (traffic-aware seconds when modelled, else free-flow), picks the byAir-truth dep/arr instant from the snapshot (live value over scheduled), and hands those to `airport_drive_inputs.departure_block` / `arrival_block`. The airport leg endpoint is the airport `name` (falling back to `code`) — what the precheck's existing time-to-leave query already routes, and what reads cleanly as the block's calendar location; the routed origin/destination pair is captured on the block so the recheck re-routes the same leg. Errors degrade per leg, never abort: a byAir lookup or Maps route failure drops just that block (the primary airport's code is required; a secondary-airport lookup failure falls back to the safe international classification), the next cycle retries. Pure of calendar I/O — given injected clients and a resolved `origin`, it returns the desired blocks; the primary-calendar fetch + `plan_drive_block` + Composio create/shift executor, and the precheck moving-origin re-anchor, land in the follow-up PRs on #90.

### Added — airport drive block input builder, groundwork for the integration (`jbaruch/nanoclaw-travel#90`)

flight-assist gains `airport_drive_inputs.py` — the pure, deterministic seam between the live world (byAir airport context + Maps routing + the resolved origin) and the `airport_drive.plan_drive_block` planner. Given a flight's already-fetched dep/arr `get_airport` payloads, the two airport codes, the byAir-truth dep/arr instants, the routed leg (origin/destination/baseline seconds), and the optional `config.json` clearance overrides, it builds the two `DesiredDriveBlock`s the planner consumes: the departure block anchored to the be-at-the-airport deadline (`dep − clearance`, the route-class buffer plus the departure airport's `delay.index` nudge) running `[anchor − drive, anchor]`, and the arrival block anchored to the earliest the drive home can start (`actual_arr + post_arrival_delay`) running `[anchor, anchor + drive]`. Summaries are the #90 §10 literals (`Drive: → BNA (DL123)` / `Drive: BNA → home`); the CREATE timezone is the relevant airport's IANA tz. An `airport_context` extractor pulls the flag/`delay.index`/tz slice out of byAir's raw payload defensively (a non-dict or a missing field degrades to None, never raises — an absent flag classifies international, an absent tz omits the timezone). Clearance/post-arrival math stays in `airport_lead`; this module only reads the config-override keys and passes them through (a malformed hand-edited override is ignored, the default applies). Pure (no I/O, no clock), fully unit-tested, and the test guards the seam by asserting a built block survives `airport_block.build_block_args` and parses back. Not yet wired into the precheck or reconcile; the fetch/route and moving-origin re-anchor land in the follow-up PRs on #90.

### Added — airport clearance config fields (`jbaruch/nanoclaw-travel#90`)

`config.json` gains five optional, non-negative airport-clearance fields — `airport_clearance_domestic_minutes`, `airport_clearance_international_minutes`, `airport_post_arrival_domestic_minutes`, `airport_post_arrival_intl_us_minutes`, `airport_post_arrival_intl_abroad_minutes` — the operator's risk-tolerance knobs for how early to be at the airport before departure and how long after landing before the drive home can start. Each overrides the matching `airport_lead.py` default (60 / 120 / 20 / 40 / 60); absent → the default applies. `STATE_SCHEMA_VERSION` bumps 4→5 with an additive, no-op migration (an old v4 config gains no keys). The byAir `delay.index` nudge (low/med/high → +0/+15/+30) stays an `airport_lead` constant — it's keyed on byAir's index and doesn't fit the flat int-field config shape. Not yet consumed; the precheck wiring that reads these lands in the follow-up PR on #90.

### Added — airport drive block planner, groundwork for airport drive blocks (`jbaruch/nanoclaw-travel#90`)

flight-assist gains `airport_drive.py` — the pure create/shift/skip planner for the airport drive blocks. Given a flight's already-resolved drive inputs (a `DesiredDriveBlock` the precheck will compute from the byAir airport context + Maps routing + the resolved origin), it finds this flight+direction's block by scanning the fetched calendar events for its `[flight-assist:flight=<id>:dir=<dir>]` marker — **no local ledger** (calendar-as-state, the drive-planner model, matching how `state-schema.md` documents the blocks) — and emits 0–1 ops: create when none exists, no-op when the live window already matches, or update when a re-anchor/re-route shifted it. The op `create_args` is the `airport_block` `build_block_args` dict, ready for the executor to pass to CREATE/PATCH (`create_args["calendar_id"]` always equals the op's `calendar_id`, so the PATCH target never diverges). Pure (no I/O), two block kinds (`airport_drive_dep` / `airport_drive_arr`). It lives outside `calendar_plan.py` deliberately: those reconcile ops carry `{summary, start, end, private_props}` bodies for byAir-calendar events, whereas the airport blocks use the self-contained `airport_block` codec on the primary calendar. Not yet wired into the precheck; the I/O wiring lands in the follow-up PR on #90.

### Added — airport drive block codec, groundwork for airport drive blocks (`jbaruch/nanoclaw-travel#90`)

flight-assist gains `airport_block.py` — the calendar-as-state codec for the airport drive blocks: it builds the `GOOGLECALENDAR_CREATE_EVENT` args for a block and parses a fetched event back into a typed `BlockState`. State (schema v1) rides in the event description as a `[flight-assist:flight=<id>:dir=to_airport|from_airport]` marker plus an `<!--fadrive:{...}-->` JSON comment carrying baseline drive seconds, the anchor instant, routed origin/destination, and the alert-suppression record (the `fadrive` prefix is distinct from flight-assist's existing `<!--fa:-->` event tags, to avoid collision). Free transparency, airport-IANA-tz create, recheck-window + once-per-alert logic. A deliberately self-contained sibling of drive-planner's `block_props.py` — the shared-extraction approach was dropped as too complex for the cross-skill coupling it required (#90 decision); drive-planner is untouched. Not yet wired into block creation; the integration lands in the follow-up PR on #90.

### Added — byAir airport-context client methods, groundwork for airport drive blocks (`jbaruch/nanoclaw-travel#90`)

`byair_client` gains `get_airport(airport_id)` and `get_airport_tips(airport_id)` — the airport context the drive blocks need: `countryName`/`countryFlag` for international classification, the structured `delay` index for the congestion nudge, the IANA `timezone` for correct block placement, and free-text community tips for the reasoning layer. Both cache per airport id for the client's lifetime: byAir throttles ~10 calls/session and a single precheck cycle queries the same departure/arrival airports across flights, so repeats are served from cache without spending a call. `_call_tool`/`_tools_call` return types are corrected to `Any` (some byAir tools — `byair_get_airport_tips` — return a JSON array, not an object). Not yet wired into block creation; the integration lands in follow-up PRs on #90.

## 0.1.50 — 2026-06-25

### Added — airport clearance resolver, groundwork for airport drive blocks (`jbaruch/nanoclaw-travel#90`)

New `airport_lead.py` (sibling of `boarding_lead.py`) resolves the two ground-transit deadlines around a flight: how early to be at the airport before departure (domestic 60 / international 120 min, nudged up by byAir's airport `delay.index`), and how long after landing before the drive home can start (domestic 20 / intl-to-US 40 / abroad 60 min). International vs domestic is decided by decoding each airport's `countryFlag` emoji to its ISO 3166-1 alpha-2 code (byAir exposes no ISO field, only a native-spelling `countryName`) and matching a canonical Schengen set, so intra-Schengen counts as domestic. An undecodable flag falls back to the international (larger) buffer. Pure, config-overridable, fully unit-tested; not yet wired into block creation — the integration lands in follow-up PRs on #90.

## 0.1.49 — 2026-06-25

### Fixed — drive-planner cancel UX: by list number or name, never an internal id (`jbaruch/nanoclaw-travel#86`)

The sweep notification told the operator to "Reply `skip <meeting_id>`", where `meeting_id` was the raw Google Calendar event id (opaque base32, effectively untypeable) — internal plumbing leaked to the user. Now the user-facing surface never carries an id: when one block is added the notification offers a plain "reply `skip` to cancel"; when several are added it numbers them and offers "`cancel 2`" / "`cancel 1,3`". A new `apply.py list` mode returns the current drive blocks (one per meeting, ordered by leave-by, summary stripped of the "Drive: " prefix) with their internal `meeting_id`s, so the cancel step maps the operator's ordinal or meeting name onto the id itself and confirms by name. The id never appears in, or is required from, a user message.

## 0.1.48 — 2026-06-25

### Fixed — drive-planner no longer plans impossible cross-city ground drives (`jbaruch/nanoclaw-travel#85`)

The sweep bridged any two consecutive in-person meetings within the tight-gap window by clock gap alone — so a St. Louis conference talk (flown to) chained to a Brentwood TN swimming practice produced a 309-min "drive" inside a 45-min gap, and the flown-to talk itself drew a ~4.5h ground drive. `plan_meetings` now applies two sanity gates after routing: a bridge whose routed drive overruns the gap between the meetings, or any leg whose drive exceeds `MAX_REASONABLE_DRIVE_SECONDS` (3h — the operator almost certainly flew), is recorded under a new per-meeting `unplannable` list with a human reason instead of becoming a block. The leg is surfaced, never silently dropped (§5): the SKILL.md tells the operator "no drive block for X — likely flying". Flight/TripIt awareness (knowing where the operator physically is) stays a future enhancement; this gate catches the nonsensical output regardless.

## 0.1.47 — 2026-06-25

### Fixed — calendar blocks land at the right instant: explicit CREATE timezone (`jbaruch/nanoclaw-travel#83`, `#82`)

Live verification of the *placement* (not just the description round-trip) showed drive blocks landed ~5h early: the live `GOOGLECALENDAR_CREATE_EVENT` reads a bare `start_datetime`'s wall-clock as **UTC** unless an explicit `timezone` is supplied, so an offset-bearing string alone is mis-anchored (created events came back stamped `timeZone: UTC`). The earlier flat-create fix (0.1.46) corrected the duration half of #83 but not this timezone half.

- **drive-planner** threads the meeting's IANA `start.timeZone` (which live Google events carry) from `scan` → `MeetingClass` → `build_block_args`, emitted as the CREATE `timezone`; a block missing its IANA `timeZone` but carrying an offset falls back to a fixed-offset `Etc/GMT±N` zone. Verified live: a 14:00-CT meeting's block now lands at 13:30 America/Chicago, not 08:30.
- **flight-assist** has only the departure offset, so `calendar_reconcile` maps a whole-hour offset to a fixed-offset `Etc/GMT±N` zone (correct instant + local-clock display); a rare non-whole-hour offset (e.g. +05:30) normalizes `start_datetime` to UTC instead. This also closes the boarding-create half of #82 (the create itself was fixed in 0.1.46).

Both paths verified end-to-end against the live toolkit (create → fetch → assert wall-clock placement → delete).

## 0.1.46 — 2026-06-25

### Fixed — calendar writes rebuilt for the live Composio v3 contract (`jbaruch/nanoclaw-travel#59`)

Live NAS verification of the *write* path showed both skills' calendar I/O was built against an assumed Composio contract that does not exist on the live v3 toolkit — every create silently failed (`executed: 0`), so no blocks or boarding events were ever written. Probed every `GOOGLECALENDAR_*` action against the NAS and rebuilt to the real shapes:

- **No writable `extendedProperties`.** Neither `CREATE_EVENT` nor `PATCH_EVENT` exposes it, so the machine state both skills stamped there could never be written. drive-planner's block state (baseline seconds, arrive-by, routed endpoints, alert record) and flight-assist's managed-event tags (`faFlightId`/`faKind`/`faManaged`) both move into the event **`description`** — drive-planner as a `<!--dp:{...}-->` comment beside its `scan` marker, flight-assist as a `<!--fa:{...}-->` comment via the new `calendar_tags` codec. This supersedes the `extendedProperties.private` design described in 0.1.44.
- **Flat create/patch.** `CREATE_EVENT` takes flat `start_datetime` + `event_duration_hour`/`event_duration_minutes` (the old nested `start.dateTime`/`end.dateTime` was rejected); `PATCH_EVENT` takes flat `start_time`/`end_time`. flight-assist's adopt path now appends its tags to byAir's existing description (preserving it, stripped back off on the next read so tags never accumulate) instead of clobbering a separate field.
- **Response shapes.** `FIND_EVENT` double-nests events at `data.event_data.event_data`; `LIST_CALENDARS` returns the list under `calendars`. The old `items` reads found nothing. Both skills' `_items` walk the live shapes; drive-planner's recheck-poll suppression PATCHes the rebuilt `description`.

The internal `private_props` abstraction is unchanged end-to-end — `normalize_event` decodes the description comment back into it on read, the reconcile write helpers encode it on create/patch — so the flight-assist planner is untouched. Verified live with the real modules: create → fetch → parse round-trips for a drive block, and create → normalize → adopt-patch (description preserved) for a boarding event, each cleaned up after. Added direct create/patch arg-shape regression tests (the gap that let the nested-format bug ship) plus a `calendar_tags` codec test.

### Fixed — drive-planner never plans a drive to a declined or cancelled meeting (`jbaruch/nanoclaw-travel#59`)

`scan` filtered virtual / all-day / past meetings but ignored the operator's RSVP, so a meeting you declined still got a drive block — and `fetch_events` dropped `attendees` entirely, so the data to detect it wasn't even carried through. Live probing confirmed the shape: the operator's own attendee row carries `self: true` + `responseStatus`. `scan` now filters an event whose self-attendee is **explicitly** `declined` (and event `status: "cancelled"`); `accepted` / `tentative` / `needsAction` all still plan, and a declined meeting is excluded as a routing neighbour so it can't strip a real meeting's home legs. `fetch_events` carries `attendees` + `status` through its projection.

### Fixed — flight-assist state-validation crash + Optional-flow type bugs (`jbaruch/nanoclaw-travel#59`)

Resolving pyright across the `sys.path`-insert bundle layout surfaced real source bugs. The flight-state validators formatted type-mismatch errors with `expected_type.__name__`, but the schema dicts permit tuple types like `(dict, type(None))` — a tuple-typed field on a mismatch would raise `AttributeError: 'tuple' object has no attribute '__name__'`, masking the intended `StateError`; a `_type_name` helper now handles both shapes. `byair_client` guards a `raise __cause__` where `__cause__` could be `None`. `phase_markers`' `check_*` functions had `scheduled_*_time: str` params that already tolerated `None` internally (`_parse_iso8601` accepts `str | None`); the annotations were corrected and the fired-event appends in `precheck` guarded so a `None` event can't reach `events.append`.

## 0.1.45 — 2026-06-24

### Fixed — drive-planner calendar fetch action slug (`jbaruch/nanoclaw-travel#59`)

Live NAS verification surfaced the action-slug caveat `fetch_events.py` flagged: the sweep's calendar fetch used `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS`, which does not exist in the live Composio v3 toolkit and 404s. Corrected to `GOOGLECALENDAR_EVENTS_LIST` with the schema-required camelCase `calendarId: "primary"` + `singleEvents: true` arguments (verified against `GET /api/v3/tools/GOOGLECALENDAR_EVENTS_LIST` and matching the proven `nanoclaw-admin` `composio-fetch` precheck). Scope adjusts from the epic's aspirational "all calendars" to the **primary** calendar — the all-calendars slug isn't real, and primary is where in-person meetings live; multi-calendar fan-out (list calendars → fetch each) is a future enhancement. The `data.items` container is the Google-native events.list shape; the response-key candidates now check `items` first. Probed live against the operator's calendar (HTTP 200, 44 events with summary/location). Test asserts the slug + the `calendarId`/`singleEvents` args.

## 0.1.44 — 2026-06-24

### Added — drive-planner sweep + recheck poll, wired into the tile (`jbaruch/nanoclaw-travel#59`)

The two drive-planner skills that turn the deterministic core (scan / fetch / recheck-gate / skip-store, shipped over #59) into a live, registered capability (Epic #59 §3, §4, the confirmed create-first interaction model and the poll-based recheck model).

**Calendar IS the state (§4).** New `block_props.py` is the codec: `build_block_args()` stamps the `scan` self-marker into the block description and the machine state — baseline drive seconds, arrive-by, routed endpoints, an alert-suppression record — into `extendedProperties.private`; `parse_block()` reads a fetched event back into a typed `BlockState` with leave-by + recheck-window math. A test pins the built marker against `scan._MARKER_RE` so the two never drift. There is no local block store — the recheck poll re-derives every block from the calendar each cycle, so a recheck can never be silently forgotten (lombot #48). `fetch_events.py` now carries `extendedProperties` through its projection so the poll can read its own blocks; `scan` ignores the field it doesn't read. `next_alerts()` fires each recheck condition (traffic grew past threshold / leave-by arrived) at most once per block — re-pinging a still-grown drive every poll is the trust-eroding nag (§5 #49 in spirit).

**Sweep (`drive-planner`, ~2h cadence).** `precheck.py` is the deterministic spine: fetch the wide window → `scan` → for each `needs_decision`/`bridge`/`back_to_back` meeting, pre-route every leg with live traffic and build the exact `GOOGLECALENDAR_CREATE_EVENT` args. Routing is deterministic, so it lives in the script, not the agent; a leg the router can't price is reported, never dropped (no silent miss, §5). The SKILL.md is an action router: on a wake it runs `apply.py create` (idempotent — finds existing markers first, never double-books, lombot #50) then sends one "added drive block for X, leave by HH:MM — reply skip to remove" notification; a "skip `<id>`" reply runs `apply.py remove` (delete the blocks + record a skip so the next sweep won't recreate them, expiry derived from the block when the reply omits the meeting end).

**Recheck poll (`drive-planner-recheck`, ~15-min cadence).** `precheck.py` re-fetches the near-term window by direct API call, parses its own marked arrival-anchored blocks back off `extendedProperties`, re-routes each due leg, runs `evaluate_recheck`, and fires each condition once. It only *produces* the suppression patches (each carrying the block's full private map with the alert record updated); the recheck SKILL.md applies them via `apply.py suppress` AFTER the send confirms, so a failed send never permanently suppresses a leave-earlier / leave-now alert (a forgotten patch merely re-pings next poll — the safe direction). The SKILL.md composes the push, then records suppression. Outer-boundary prechecks fail closed; the leave-by alert is re-derived each poll, so one skipped cycle never loses it permanently.

**Home address (§4).** `home_address.py` reads `current_home` from the canonical `## Addresses` block in `/workspace/trusted/user_profile.md` (owned by the `nanoclaw-trusted` trusted-memory skill — a separate change there lands the block). It deliberately ignores `new_home_wip` and refuses to guess on a missing block — a silent wrong origin would mis-route every leg — raising an actionable error pointing at the trusted tile.

**Packaging.** Both skills registered in `tile.json`; `state-schema.md` documents the calendar-as-state block contract alongside the skip store; README skills + scripts tables updated. `maps_client` and `composio_client` are imported read-only from the co-located flight-assist bundle via the runtime-mount-with-dev-fallback pattern `sync-tripit` already uses — flight-assist's mission-critical leave-by path is untouched (zero flight-regression risk), so `maps_client` was not moved. Composio is mid-retirement (nanoclaw#638) — the API fetch + patch are the pieces that re-point later. ~50 new tests across the codec, home reader, suppression, sweep planner, apply step, and recheck poll (injected routers + a fake Composio client; no live calendar/maps). Live NAS verification + the admin address block are tracked separately under §7.

## 0.1.43 — 2026-06-24

### Added — drive-planner wide-window calendar fetch (`jbaruch/nanoclaw-travel#59`)

The live calendar read that feeds the sweep (Epic #59 §4): `skills/drive-planner/fetch_events.py`, a self-contained Composio client that makes one wide-window `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS` call over a `[time_min, time_max]` window and returns the raw Google Calendar event dicts in the exact shape `scan(events=...)` consumes (`id`, `summary`, `location`, `start`, `end`, `description`). drive-planner owns its own fetch rather than importing flight-assist's per-calendar `composio_client` (a different action, a separately-loadable skill bundle), but mirrors that module's transport faithfully: stdlib-only `urllib`, HTTP-mockable in CI, the Composio `successful`/`error` envelope, read-timeout normalized to `URLError`, one client per process. The action slug and the candidate `data` event-container keys (`events` / `items`) are isolated at the top of the file for one-line correction against the live toolkit; a `successful: true` body carrying no recognizable event list raises `FetchError` rather than silently returning zero events (which would make the sweep a no-op and quietly stop planning). Window inputs are guarded (tz-aware, `time_max > time_min`); a tool-level failure raises `FetchError` with the upstream `status_code`. 18 mocked-HTTP tests incl. an integration check that the fetched events feed `scan`. Like `maps_client`/`composio_client` it is a transport library with no CLI; the sweep precheck that composes fetch → scan lands with the SKILL.md. Composio is mid-retirement (nanoclaw#638) — this is the one piece that re-points later.

## 0.1.42 — 2026-06-24

### Added — drive-planner skip store (`jbaruch/nanoclaw-travel#59`)

The on-disk store of "skip this meeting" decisions that feeds `scan()`'s `skip_state` (Epic #59 §3, §5 #49): `skills/drive-planner/skip_state.py`, owning `<state_dir>/skip-state.json` (`{"schema_version": 1, "skips": {<id>: "<ISO expiry>"}}`). Re-asking about a meeting the user already skipped is the trust-eroding nag LoMBot hit, so a skip sticks — but with auto-expiry: the writer sets each skip's expiry to the meeting's end, and `load_active_skips(now)` drops anything expired so a stale skip never suppresses a meeting forever. API: `add_skip(id, expires=, now=)`, `load_active_skips(now)` (the `{id: expiry}` mapping `scan` consumes, read-only), `clear_skip(id, now=)`, `prune(now)`. Writes are atomic (temp-file + rename, temp cleaned in `finally`). Per `coding-policy: stateful-artifacts`: drive-planner is the sole owner, the state dir is overridable via `DRIVE_PLANNER_STATE_DIR`, and `state-schema.md` documents the schema, the writer/reader contract, and the tolerance rules — a missing file reads as "no skips", a present-but-corrupt file (bad JSON, non-object root, missing/newer `schema_version`) raises `SkipStateError` rather than silently resurrecting every skip, and malformed individual entries are dropped. 28 tests incl. an integration check that the loaded mapping drives `scan` to the `skipped` bucket. Not yet wired into `tile.json` — the SKILL.md sweep that writes and reads it lands next.

## 0.1.41 — 2026-06-24

### Added — drive-planner recheck gate (`jbaruch/nanoclaw-travel#59`)

The deterministic gate the scheduled T-45 / T-30 / T-15 rechecks use (Epic #59 §3, §5 #48): `skills/drive-planner/recheck.py`, a pure function `evaluate_recheck(baseline_seconds, current_seconds, arrive_by, now, …)` → `RecheckDecision`. Most rechecks are no-ops; pinging on every couple-minute fluctuation erodes trust, so the gate alerts only when the drive grew at least `threshold_seconds` over the baseline (default 10 min) OR the recomputed leave-by (`arrive_by − current − buffer`, default 5-min buffer) is at/before `now` — you must leave now regardless of growth. It does not route — `current_seconds` comes from live traffic (`maps_client`) upstream, the gate's caller's job — so the function stays pure and fully testable. The CLI follows the precheck-gating contract (`coding-policy: script-delegation` Precheck Gating): stdin JSON request → stdout `{"wake_agent": <alert>, "data": {<decision>}}`, so a scheduler runs it and only wakes the agent on an alert, with `data` carrying the delta and recomputed leave-by for the ping. All boundary inputs are validated (non-negative integer durations, tz-aware datetimes with `Z`-normalization and naive rejection) with `RecheckError` → JSON stderr + non-zero exit, matching the scan classifier's hardening. 36 tests (alert triggers, silence cases, leave-by math, input guards, CLI contract); no live routing. Not yet wired into `tile.json` — the SKILL.md that schedules and consumes rechecks lands with the sweep.

## 0.1.40 — 2026-06-24

### Added — drive-planner scan classifier (`jbaruch/nanoclaw-travel#59`)

The deterministic brain of the new `drive-planner` skill (Epic #59 §3, §5): `skills/drive-planner/scan.py`, a pure events-JSON → buckets classifier. Given the wide-window calendar events plus `now`, the home address, and the skip-state, the pure `scan()` returns one `MeetingClass` per event in one of the buckets `needs_decision` / `bridge` / `back_to_back` / `has_block` / `skipped` / `past` / `filtered`, plus the concrete `TransitLeg`s each routable meeting needs (outbound / return / bridge with deadlines). It does not route — drive time needs live traffic (`maps_client`) downstream — so bridge legs expose `gap_seconds` for the router's drive-time > gap warning. Every scar from LoMBot's 16 closed `drive_planner` issues is baked in: handled = ANY marker, not both directions (#50); skips persist with expiry and virtual locations are filtered, never asked (#49); past guard everywhere, including exclusion from neighbour linking so a stale same-venue meeting can't strip a future meeting's outbound leg (#28 × #14/#7); neighbour-aware same-venue-tight = back_to_back vs different-venue-tight = bridge (#14/#7); whitespace-normalized location before routing (#37); return and bridge first-class (#2/#40). Datetime parsing normalizes a trailing RFC3339 `Z` and rejects timezone-naive values (which would raise against the tz-aware `now`). Nothing is silently dropped — filtered/past events come back with a reason so the sweep can audit. The module also ships a CLI entry point (stdin JSON request → stdout `{"results": […]}`, stderr + non-zero on bad input) so the deterministic operation is a runnable script per `coding-policy: script-delegation` / `file-hygiene`, while the pure `scan()` stays the unit-tested core. 39 fixture tests (no live calendar), each neighbour/idempotency/skip/past case named after the lombot issue it encodes. The skill is not yet registered in `tile.json` (no `SKILL.md` until the fetch + recheck pieces land); this is the classifier slice only.

## 0.1.37 — 2026-06-23

### Added — TomTom backup routing in `maps_client` (`jbaruch/nanoclaw-travel#59`)

`maps_client` gained a Google-primary → TomTom-backup chain behind the unchanged `travel_time() → TravelTime` interface, the first piece of the drive-planner epic (#59) and a hardening of the existing flight `time_to_leave`. Google Distance Matrix stays primary; on any Google `MapsError` or transport (`URLError`) failure, the client falls back to TomTom when `TOMTOM_API_KEY` is configured. TomTom routing is coordinates-only, so the new `TomTomClient` does geocode-origin → geocode-destination → route-with-`traffic=true`, mapping `noTrafficTravelTimeInSeconds` / `travelTimeInSeconds` onto the same free-flow / in-traffic split Google returns. `TravelTime` gained a `source` field (`"google"` / `"tomtom"`) so callers can tell which provider answered. There is deliberately no no-traffic fallback (e.g. OSRM) — a duration without a live-traffic model is false confidence for a leave-by deadline; when both providers fail the client raises `MapsError("ALL_PROVIDERS_FAILED", …)` naming what each reported. `MapsClient.from_env` wires the backup only when `TOMTOM_API_KEY` is set, so a Google-only deploy is unchanged. The caller in `precheck.py` already catches `MapsError` + `URLError`, so the fallback integrates with no caller change. 16 new tests (TomTom geocode+route success, no-baseline traffic split, geocode/route zero-results, full Google→TomTom fallback on both `MapsError` and `URLError`, combined-failure error, Google-success-skips-TomTom, `from_env` wiring with/without the key); `.env.example` documents the optional key.

## 0.1.36 — 2026-06-22

### Added — calendar teardown tombstone sweep + wake-cycle wiring (`jbaruch/nanoclaw-flight-assist#55`)

The final reconciliation slice: switched-away flights now get their managed calendar events torn down, and the reconcile runs on the wake cycle. `calendar_reconcile.run_reconcile` gained a second pass — a tombstone sweep over on-disk flights that have dropped out of `active-flights.json` but still carry a `calendar_events` ledger. The per-flight wake loop only visits active flights, so this sweep is the only place a switched-away flight's stale events (which byAir leaves behind) get deleted. It resolves each tombstone's disposition off the retained ledger (switched_away / cancelled / diverted → teardown deletes; completed → leave the events as a historical record), executes the deletes, then **archives** (removes) the state file once teardown settles — every delete succeeded, or the flight has completed. A failed delete keeps its ledger entry, so the tombstone is retained for the next cycle's retry rather than archived with events still live. The summary gains an `archived` count. Teardown is ledger-driven, so when there are no active flights the cycle skips the calendar fetch entirely.

For the tombstone to survive, `sync_tripit._reconcile_active_flights` now retains `flight-<id>.json` (instead of deleting it on upstream removal) when the record still holds a non-empty `calendar_events` ledger; a removed flight with nothing to tear down is still deleted immediately. `state.py` gained `list_flight_state_ids()` to enumerate on-disk per-flight files regardless of active-flights membership — the sweep needs to see exactly the flights the index no longer lists.

`SKILL.md` Step 3 became "Handle the precheck wake cycle": it runs `scripts/reconcile.py` first (idempotent, delta-only, safe alongside byAir's own delay-shifts), then composes the notification. `no_calendar` / `no_flights` / missing-Composio-credentials all mean reconciliation is inactive this cycle and are handled silently — calendar reconciliation stays optional. 11 new tests (sweep teardown + archival, completed-leaves-events, failed-delete retention, empty-ledger non-tombstone, active+tombstone in one cycle, `list_flight_state_ids`, sync_tripit tombstone retention vs immediate delete).

## 0.1.35 — 2026-06-22

### Added — calendar reconcile orchestrator (`jbaruch/nanoclaw-flight-assist#55`)

The I/O layer that connects the pure planner to live Google Calendars (#55). `calendar_reconcile.py` resolves the calendar IDs, fetches + normalizes the current calendar state via Composio, builds the per-flight planner inputs (disposition via `disposition.py`, boarding lead via `boarding_lead.py`, byAir-truth dep/arr times), runs `plan_reconciliation`, executes the returned ops (`create` / `update` / `adopt` / `delete` / `forget`) through `composio_client`, and writes the owned event IDs back into each flight's `calendar_events` ledger. `scripts/reconcile.py` is the wake-cycle entry point: it emits a single-line JSON summary (`status` ∈ `ok` / `no_calendar` / `no_flights`) and collects per-op Composio failures rather than aborting the cycle — a delete that 404s is an idempotent success, a real failure defers that op to the next cycle.

The flight ("Flighty Flights") calendar ID is resolved at runtime from the operator-supplied `byair_calendar_name` and cached, never hardcoded in tile code per `rules/flight-data-locality.md`; Reclaim travel blocks live on the **primary** calendar (content-classified). The exact `GOOGLECALENDAR_*` argument field names are isolated in one section for live-toolkit verification, the same treatment `composio_client.py` gives its action slugs. This slice reconciles the flights in `active-flights.json`; the tombstone sweep for switched-away flights lands next (the planner already emits teardown ops for cancelled / diverted flights still in the index, which this executes). 18 orchestration tests against a fake Composio client (resolution, create/adopt/teardown, delta no-op, 404 idempotency, Reclaim same-airport-gap delete, malformed-event skipping) plus 2 CLI-contract tests.

### Added — state schema v4: cached flight-calendar id in config (`jbaruch/nanoclaw-flight-assist#55`)

`config.json` gains two optional calendar-reconcile fields: `byair_calendar_name` (operator-supplied display name of the flight calendar) and `byair_calendar_id` (the id the reconcile caches after its first name match). Both are optional and absent-tolerant, so the v3→v4 owner-side migration only bumps `schema_version` — no shape change to config, active-flights, or per-flight records. See `state-schema.md`.

### Fixed — precheck preserves the calendar_events ledger (`jbaruch/nanoclaw-flight-assist#55`)

`precheck._build_flight_state` rebuilt the per-flight record from scratch on every poll and dropped `calendar_events`, which would have wiped the reconcile-owned ledger (and the teardown tombstone it doubles as) every ~2 minutes. It now carries the ledger forward verbatim from prior state, so the reconcile's writes survive subsequent polls.

## 0.1.34 — 2026-06-22

### Added — calendar event normalization + Reclaim travel classifier (`jbaruch/nanoclaw-flight-assist#55`)

The read-side adapter for calendar reconciliation (#55), built against the real Google Calendar event shapes. `calendar_normalize.py` flattens a Google event resource into the planner's `{event_id, calendar_id, summary, start, end, private_props, is_reclaim_travel}` shape, and classifies Reclaim-generated travel blocks.

`is_reclaim_travel` is **content-based, not calendar-based**: there is no dedicated Reclaim calendar — Reclaim writes its travel blocks onto the user's primary calendar interleaved with real meetings, so the only safe delete discriminator is the event's own content. Two factors, both required: the Reclaim authorship signature (`app.reclaim.ai`) in the description AND a travel marker in the summary (`🚌 Travel`). Reclaim's habit/focus/task blocks carry the signature but a different summary → not flagged; a user's own event titled "Travel" carries no signature → not flagged. The planner further bounds every delete to a same-airport layover gap, so a genuine meeting is never a candidate. `calendar_id` comes from the fetch context (authoritative), not the event body; `private_props` is `extendedProperties.private`.

### Fixed — whitespace-insensitive flight-code adopt match (`jbaruch/nanoclaw-flight-assist#55`)

Real Flighty flight-event summaries render the code with a space (`✈ BNA→YYZ • UA 8018`) while byAir's `code` field may carry it unspaced (`UA8018`), so the planner's `code in summary` adopt-match missed. `_match_byair_event` now strips whitespace from both sides before comparing, matching regardless of which side carries the space.

## 0.1.33 — 2026-06-22

### Added — flight disposition resolver (`jbaruch/nanoclaw-flight-assist#55`)

Next deterministic slice of calendar reconciliation (#55): `disposition.py` resolves each flight's reconciliation disposition (`active` / `cancelled` / `diverted` / `switched_away` / `completed`) that `plan_reconciliation` consumes to decide between normal reconcile, teardown, and leave-as-record. The computation needs the two inputs the pure planner deliberately stays out of — the wall clock and `active-flights.json` membership — so it lives in one isolated, tested module, the same carve-out as `boarding_lead.py` keeping volatile policy out of the planner.

Precedence: byAir `computed_status` cancelled/diverted wins over membership and time; `landed` or an effective-arrival instant at/before `now` is `completed`; a flight that has dropped out of active-flights while still in the future is `switched_away` (the per-flight wake loop can no longer see it — teardown runs off the retained ledger tombstone); everything else in active-flights and not yet arrived is `active`. Effective arrival prefers byAir's actual `last_snapshot.arr_time` over `scheduled_arr_time`, so a delayed in-air flight stays `active` until it actually lands. 16 tests cover the precedence matrix, the actual-vs-scheduled arrival boundary, null/missing snapshots, and the RFC-3339 offset handling.

## 0.1.32 — 2026-06-22

### Added — Composio calendar transport client (`jbaruch/nanoclaw-flight-assist#55`)

The I/O layer the pure planner (#55, 0.1.31) needs to execute its op list. `composio_client.py` is a thin stdlib-`urllib` REST client over Composio's v3 `tools/execute/{action}` endpoint, mirroring `byair_client.py` / `maps_client.py` (HTTP-mockable in CI, one client per process). It injects `x-api-key` auth + `COMPOSIO_USER_ID` scoping, names the `GOOGLECALENDAR_*` action slug, and passes a Composio-shaped `arguments` dict through — the planner-op → arguments mapping (and the version-specific per-action argument schemas) stays with the reconcile executor that lands next, where it is verified against the live toolkit.

The Composio envelope returns HTTP 200 even on a tool-level failure (`successful: false`), so the client raises `ComposioError` on that and surfaces the upstream provider status in `.status_code` — a delete that 404s (event already gone) is distinguishable from a real failure, letting the executor treat it as an idempotent no-op. HTTP-level failures (bad key, 5xx) propagate as `urllib.error.HTTPError`; a body-read timeout normalizes to `URLError` (mirrors byair, #28). 15 HTTP-mocked tests cover request shaping, the success/failure envelope, status-code surfacing, and transport-error normalization.

`check-env.py` now also reports `composio_key_present` / `composio_user_present` (SKILL.md Step 1 + tests updated to match), and `.env.example` documents `COMPOSIO_API_KEY` / `COMPOSIO_USER_ID` (plus the optional `COMPOSIO_BASE_URL` override). No wake-cycle wiring yet — the reconcile script that fetches events, runs the planner, and writes the ledger back lands in the follow-up.

## 0.1.31 — 2026-06-20

### Added — pure calendar reconciliation planner + boarding-lead resolver (`jbaruch/nanoclaw-flight-assist#55`)

The deterministic core of calendar-event reconciliation (#55), built as two pure, network-free modules so the whole decision surface unit-tests in CI per `coding-policy: script-delegation`. The Composio I/O layer that executes the plan lands in a follow-up.

`calendar_plan.py` — `plan_reconciliation(flights, events, config)` takes the per-flight `calendar_events` ledger plus a normalized snapshot of what is on the byAir and Reclaim calendars, and emits a declarative op list (`create`/`update`/`delete`/`adopt`/`forget`) that converges the calendar to the desired state. Delta-only: it no-ops when a live event already matches the `synced_signature`, so it is safe to run alongside byAir's own shifts (no stomping). Covers all four behaviors: boarding-block lifecycle, byAir flight-event adopt-by-tag-then-shift, the positional Reclaim same-airport-layover deletion rule, and teardown of managed events on a cancelled/diverted/switched flight. Event classification is by calendar ID (no summary regex), so user-created events are never touched and only Reclaim-calendar blocks in a same-airport gap are deleted.

`boarding_lead.py` — `resolve_boarding_lead_minutes(...)` encodes the (volatile) boarding-pace policy in one isolated, tested place; the planner consumes the resolved integer only. Policy: transoceanic crossing → 50, widebody → 50, narrowbody → 30, nothing classifiable → 30. Aircraft size is by aisle count (A320 family incl. A321, all 737, 757, regional/turboprop are narrowbody; twin-aisle is widebody), from byAir's top-level `model` with a fallback to `inbound.aircraft_model`. Transoceanic (TATL/TPAC) detection is a longitude-block + great-circle-distance heuristic over airport lat/lon — no country/continent table — and correctly excludes Europe↔Asia overland long-haul.

36 new tests (`test_calendar_plan.py`, `test_boarding_lead.py`) cover boarding create/no-op/shift/recreate, adopt/skip-tagged/tolerance/shift/forget, Reclaim delete-vs-keep across the positional cases, teardown, and the full lead-policy matrix on real airport coordinates. Also renames the `Flighty` references the v3 state-schema docs introduced to `byAir` per `rules/flight-data-locality.md` (byAir is the tile's single anonymized flight upstream — it both serves the data API and writes the flight events to the writable calendar); the boarding/flight calendar ID is operator config resolved at runtime, not hardcoded.

### Changed — renamed tile `jbaruch/nanoclaw-flight-assist` → `jbaruch/nanoclaw-travel`

The tile is broadening from flight-only notifications into a general travel assistant — ground-transit drive planning (borrowed from the `ligolnik/lombot` `drive_planner` design) lands as a sibling skill next. Repo and tessl registry identity rename to `jbaruch/nanoclaw-travel`; consumers update their `additionalTiles` entry to the new name. Historical CHANGELOG issue references keep the old `nanoclaw-flight-assist#NN` form — GitHub redirects them after the repo rename.

## 0.1.30 — 2026-06-19

### Added — per-flight `calendar_events` ledger + state schema v3 (`jbaruch/nanoclaw-flight-assist#55`)

Foundation for calendar-event reconciliation (#55): flight-assist is moving from a notification-only tile to one that writes Google Calendar events (a flight-assist-created boarding block, adopted byAir flight events, Reclaim travel-block cleanup). To update and delete those events in O(1) across the `*/2` precheck cadence — and to tear them down after a flight drops out of `active-flights.json`, where the per-flight wake loop can no longer see it — the per-flight state record needs a ledger of the event IDs flight-assist owns.

`STATE_SCHEMA_VERSION` bumps `2 → 3`. Per-flight `flight-<id>.json` gains an optional `calendar_events` map keyed by event kind (`boarding`, `flight`); each entry carries `event_id`, `calendar_id`, `managed` (`created`/`adopted`), and a `synced_signature` (`<start>/<end>`) the planner diffs against to no-op when the live event already matches byAir truth. `state.py` validates the field structurally (object) only — the per-entry shape is owned and deep-validated by the reconcile planner that lands in a follow-up, the same split as `last_snapshot` ↔ `byair_client`. `_migrate` now chains its version steps (a v1 record runs v1→v2→v3 in one owner-side read), adding `calendar_events: {}` to per-flight records on the v2→v3 step and bumping config/active-flights with no shape change. New `test_state.py` cases cover the v2→v3 per-flight add, the config/active-flights version-only bump, the chained v1→v3 path, round-trip with `calendar_events` present, and structural rejection of a non-object value. No behavior change yet — the precheck and SKILL surfaces are untouched; this is the state contract the reconciler builds on.

## 0.1.29 — 2026-06-19

### Fix — `boarding_started` no longer trusts byAir's premature `boarding` label (`jbaruch/nanoclaw-flight-assist#54`)

byAir flips `computed_status` to `boarding` up to ~1h before boarding actually starts, while its own `computed_status_detail` still reads "Boarding starts in N min" and `computed_phase_progress` is 0 — an internally contradictory payload (DL4662 fired a false "boarding now" alert twice, 2026-06-13 and 2026-06-16, while the flight was delayed 67 min and boarding had not begun). `detect_wake_events` no longer fires on the `computed_status` label alone: a new `_is_real_boarding` helper requires the `boarding` status AND a `computed_status_detail` that is not a future-tense "Boarding starts in …" countdown. The boarding transition is now computed against this real-boarding signal on both the prior and current snapshots, so a flight byAir prematurely marked `boarding` still fires once the detail flips to genuine boarding — even though the raw `computed_status` never changes across that flip. The upstream contradiction is byAir's (operator filed a support ticket 2026-06-16); this is the skill-side guard. Four new `test_wake_rules.py` cases cover premature-label suppression, the deferred real-boarding fire, a genuine non-future-detail boarding, and first-cycle premature suppression.

### Changed — per-skill `agentModel:` tier-down (`jbaruch/nanoclaw#613`)

Pin cadence-skill models via `agentModel:` frontmatter so they stop defaulting to Opus: **Sonnet** (`claude-sonnet-4-6`) for `flight-assist` — itinerary/flight reasoning matters there; **Haiku** (`claude-haiku-4-5-20251001`) for the data-sync skills `nightly-travel-sync` and `sync-tripit`. Part of the #613 Claude tier-down.

### Fix — `nightly-travel-sync` ran-marker carries the `<slot_key>` date the #581 watchdog expects (`jbaruch/nanoclaw-flight-assist#51`)

The skill's final-turn marker emitted `nightly-travel-sync ran: clean`/`: surfaced` with no date slot, so the #581 silent-success watchdog — which parses `task_run_logs.result` for `ran <YYYY-MM-DD>:` — classified a healthy run as `EMPTY (FRESH)` instead of `PASS`. The format only became observable after #45 fixed the underlying `sync-tripit.sh` failure that previously masked it. The marker now mirrors the sibling `nightly-cfp-sync` / `nightly-order-sync` template: `nightly-travel-sync ran <slot_key>: clean` (or `: surfaced`), where `<slot_key>` is today's UTC date in `YYYY-MM-DD` form. SKILL.md-only edit; no code change.

### Fix — ship `sync-tripit.sh`, the host-op wrapper missed in the #318 migration (`jbaruch/nanoclaw-flight-assist#45`)

The #299/#318 split moved `nightly-travel-sync`'s three Python travel-source scripts into this tile (PR #42) but dropped `sync-tripit.sh`, the wrapper the `mcp__nanoclaw__sync_tripit()` host op resolves as `<groupDir>/scripts/sync-tripit.sh`. With no skill shipping it, fresh container spawns land without the file and the host op fails with `sync-tripit.sh not found` — surfaced in `nightly-travel-sync` Step 1, already broken in `telegram_swarm` (`telegram_main` only still worked off a stale Apr-27 copy a fresh spawn would lose). This adds the wrapper under `skills/nightly-travel-sync/scripts/` — the bundle whose Step 1 invokes the op — `cd`-ing into the globally-installed `reclaim-tripit-timezones-sync` and running `node sync.mjs sync --output=json` under `set -euo pipefail`. The package is an orchestrator-image global (`Dockerfile.orchestrator`, jbaruch/nanoclaw) that a skill bundle can't declare itself, so the wrapper guards for it and exits with an actionable message naming the install site rather than a bare `cd` error when it's absent. Scripts ship with their skill dir, so no manifest edit. A smoke/contract test (`tests/test_sync_tripit_script.py`) locks the host-op contract: the script exists, is executable valid bash, runs under strict mode, invokes the sync entrypoint, and fails loudly when the package is missing.

### Fix — `wake_rules.py` detection gaps: pre-existing schedule slip + inbound-delay retraction (`jbaruch/nanoclaw-flight-assist#46`, `jbaruch/nanoclaw-flight-assist#48`)

Two symmetric blind spots in `detect_wake_events`, both leaving the operator with a stale read of a flight:

- **#46 — pre-existing schedule slip never fired `delay`.** Delay detection was purely a delta between consecutive `dep_time` polls, so a delay already baked into the *first* snapshot never surfaced (KL1017 AMS→LHR sat at `scheduled+31min` across every poll with no prior `dep_time` to delta against; `last_wake_at` stayed null). `detect_wake_events` now takes the flight's `scheduled_dep_time` (a top-level state field, not part of the `last_snapshot` shape) and, on the first cycle only, fires a `delay` (with `schedule_slip: True`) when `dep_time` slips ≥ threshold past schedule. First-cycle-only gating means the persistent slip surfaces once and the delta rule owns every later shift, so it can't re-fire each poll. `precheck.py` resolves `scheduled_dep_time` before the wake-rule call and passes it through.

- **#48 — no event when an inbound-delay prediction walked back.** `inbound_delay_predicted` fired on the way up but nothing fired on the way down, so after byAir escalated DL59's inbound to "connection missed, rebook now" and then retracted the prediction to `null` (both legs ultimately landed early), the last surface the operator saw for hours was "rebook now" — silence read as "still bad". A symmetric `inbound_delay_retracted` event now fires when a previously-surfaced prediction (≥ threshold) walks back below threshold or to null, carrying `prev_delay_minutes`/`new_delay_minutes` so the agent can compose an all-clear. Mutually exclusive with the prediction rule.

14 new `test_wake_rules.py` cases cover both: first-cycle slip at/above/below threshold, on-time, early, missing `scheduled_dep_time`, the persistent-slip no-re-fire guarantee; and retraction to null, below threshold, inbound-block-absent, partial-walk-back-still-above-threshold (no retraction), prior-below-threshold (nothing to retract), first-cycle, and prediction/retraction mutual exclusion.

### Test — restore the #41 lodging-pairing regression tests (`jbaruch/nanoclaw-flight-assist#41`)

The #41 fix in `refresh-travel-schedule.py` (keep a past `Check-in:` whose matching `Check-out:` is still live, paired by trip-ID + hotel) shipped via the #318 extraction, but its four regression tests were dropped in transit — the fix landed uncovered in 0.1.22. This restores `test_lodging_checkin_retained_while_stay_live`, `test_lodging_fully_past_stay_dropped`, `test_lodging_checkin_not_rescued_across_trips`, and `test_lodging_pairing_requires_trip_id`, which lock the pairing behaviour against regression. No production-code change.

### Added — operator-local-tz phrasing for flight-assist surfaces (`jbaruch/nanoclaw-admin#305`)

Companion to admin#305, which fixed maintenance surfaces (`heartbeat`, `morning-brief`) to phrase relative dates in the operator's timezone but left the flight-assist `day_before` surface — the one whose 2026-05-24 incident ("leg 1 today" at 21:36 the night before, container UTC already rolled to the next day) prompted the issue — to a separate fix. This is that fix.

New `rules/operator-local-tz-phrasing.md` (steering, `alwaysApply`) requires every relative-date word a flight-assist surface composes ("today" / "tomorrow" / "a travel day") to be labeled against the operator's local date. New `skills/flight-assist/scripts/read-current-tz.py` resolves `current_tz` from the host `tz_state` singleton at `/workspace/store/messages.db` (mounted RW in main/trusted containers). The overlay reads that store directly rather than admin's `heartbeat-precheck.json`, so it carries no `nanoclaw-admin` dependency; it fails open to `available: false` on any miss (missing DB/row, empty column, unsupported `schema_version`, unparseable zone) so a notification still fires with explicit-date phrasing. `home_tz` is deliberately not a fallback — relative-date phrasing needs where the operator is now.

Scope is narrow: only the today/tomorrow wording. Displayed flight clock times stay in the airport-local zone byAir provides (`flight-data-locality` / byAir's "show as-is, don't convert" contract) — the rule never converts a departure/arrival time. SKILL.md Step 3 routes the `day_before` and arrival/delay/time-to-leave surfaces through the rule. Unit tests cover the reader's resolve + every degrade path.

### Added — `nightly-travel-sync` bundle finishes the #299 reader-without-writer split (`jbaruch/nanoclaw-admin#318`)

#299 moved `check-travel-bookings` (the reader of `travel-db.json`) into this tile but left the **writers** behind in `nanoclaw-admin`'s `nightly-external-sync` bundle, so every chat loading the flight-assist overlay still required `nanoclaw-admin` just to refresh the data it consumes. This extracts the remaining travel-source scripts and the bundle steps that drive them into a flight-assist-owned skill.

New `skills/nightly-travel-sync/`:
- `SKILL.md` — daily bundle (TripIt → Reclaim sync, refresh `travel-schedule.json`, two-tier Gmail freshness probe, rebuild `travel-db.json`, run `check-travel-bookings`). Independently scheduled via `cadence:`+`script:` frontmatter — it materialises its own `scheduled_tasks` row and no longer depends on the admin bundle or admin's `resumable-cycle` machinery. A step failure surfaces a note and finishes; the daily cron + freshness probe recover the next run.
- `precheck.py` — gates the wake to a 3-day cadence anchored on `travel-db.json` mtime (the bundle's terminal artifact, the file downstream consumers read). No separate cursor file, so the gate adds no self-owned state. Fails open (wake) on internal error so a transient stat error can't freeze the pipeline.
- `scripts/refresh-travel-schedule.py`, `filter-tripit-bookings.py`, `check-travel-freshness.py` — moved from `nanoclaw-admin/skills/nightly-external-sync/scripts/`, reformatted for this tile's ruff config (double quotes, bugbear `B` enabled — the ICS-field/datetime helpers were hoisted out of the parse loop to satisfy `B023`). The admin bundle's `sync-tripit.sh` was a zero-reference orphan that shelled out to an npm package present only in the orchestrator container, not the agent container; it was dropped rather than carried as dead code (Step 1 uses `mcp__nanoclaw__sync_tripit`, the IPC-integrated path the admin bundle already used).
- The admin bundle's `references/two-tier-probe.md` was **not** carried over — as a loaded reference it was almost entirely rationale + restated filter behavior, which `coding-policy: context-writing-style` / `script-as-black-box` keep out of loaded artifacts. Its one executable directive ("never alert on `travel-schedule.json` mtime alone; escalate only on a `stale` status plus a matching TripIt forwarded-confirmation email") now lives inline in SKILL.md Step 3. Archived motivation: bare-mtime alerting was a false-positive engine — a stale `travel-schedule.json` usually just means no travel was booked recently (confirmed 2026-04-25, "Не, я просто давно не букал травел." — "I just haven't booked travel in a while"), which trained the operator to dismiss the channel. The classification detail the reference used to enumerate — TripIt Pro alerts, friend-shared trips, geofenced arrival marketing, and platform announcements are all excluded, only the forwarded-confirmation subject matches — lives solely in `filter-tripit-bookings.py` (`PREFIX`).

The `refresh-travel-schedule.py` extracted here carries the **#41 lodging fix** (keep a past `Check-in:` whose matching `Check-out:` is still live, paired by trip-ID + hotel) plus its four regression tests, superseding the in-flight admin PR #317. Step 3's Gmail fallback discovers `GMAIL_FETCH_EMAILS` inline via `COMPOSIO_SEARCH_TOOLS` rather than depending on an admin steering rule, keeping the bundle self-contained. Tests + conftest fixtures (`refresh_travel_schedule`, `filter_tripit_bookings`, `check_travel_freshness`, `nightly_travel_sync_precheck`) moved alongside the scripts. `travel-schedule.json` / `travel-db.json` stay at `/workspace/group/`, so admin's cross-tile readers (`check-orders`, `morning-brief`) are unaffected.

### Fix — size the precheck poll-loop headroom for the Maps call, not just byAir (`jbaruch/nanoclaw#562`)

Follow-up to #36's wall-clock budget. `execfile-error` kills kept recurring at ~34s (2026-05-27, 2026-05-29) — surfaced again while tracing the heartbeat wake-storm in `jbaruch/nanoclaw#562`, because each transient flight-assist crash pins heartbeat's 24h task-failure window open. #36 set `_CYCLE_POLL_HEADROOM_SECONDS = 10s`, reserved before the 30s hard-kill for "one in-flight poll" — but it only counted the byAir poll (8s) and ignored the Maps `travel_time` query that `_process_flight` runs on top of it. `_maybe_maps_client` instantiated `MapsClient.from_env()` with its 10s default, so a flight started just under the budget ran byAir (8s) + Maps (10s) ≈ 18s and overran the kill.

The Maps client now takes the same bounded per-call timeout as byAir (`_MAPS_CALL_TIMEOUT_SECONDS = 8.0`), and `_CYCLE_POLL_HEADROOM_SECONDS` is derived from `byair + maps + interpreter-teardown` (20s, leaving a 10s start-budget) so the headroom is correct by construction if either timeout changes. Regression coverage: `test_run_cycle_passes_bounded_per_call_timeout_to_maps_client` pins the kwarg; `test_poll_headroom_covers_byair_plus_maps_worst_case` asserts the headroom ≥ byAir + Maps.

### Changed — cap the precheck poll horizon at 24h (`jbaruch/nanoclaw-flight-assist#38`)

Root-cause follow-up to #36. The live index tracked 25 active flights with departures spread out to ~44 days, all polled on the 30-min `scheduled` cadence; their `last_polled_at` values cluster, so large batches (e.g. 17 flights) come due in a single cycle and the sequential byAir polls are what race the 30s execFile kill. `_due_for_poll` now skips any flight whose seeded `scheduled_dep_time` is more than `_POLL_HORIZON_HOURS = 24` away — it stays in `active-flights.json` (sync keeps the roster) but costs no byAir call until it crosses T-24h, at which point the first in-window poll fires `day_before`. The horizon clips nothing: T-24h is the earliest precheck event, and `connection_risk` already gates leg-1 on its own 24h lookahead and falls back to `scheduled_arr_time` for legs without a live snapshot, so horizon-skipped flights remain no-ops there. This shrinks the per-cycle poll batch at the source rather than only bounding it after the fact (#36's wall-clock budget remains the safety net). Regression coverage: `test_poll_horizon_skips_flight_departing_beyond_24h`, `test_poll_horizon_polls_flight_just_inside_24h`.

### Fix — bound `_run_cycle` to a wall-clock budget so slow multi-flight cycles don't trip the 30s kill (`jbaruch/nanoclaw-flight-assist#36`)

AyeAye flagged recurring `precheck script failed: execfile-error` on the `tessl__flight-assist` scheduled task (~5 fires over 4 days, each self-recovering next cycle), with every error row clustered at ~34–35s duration. Root cause: `_run_cycle` polls active flights sequentially, and #28 bounded each byAir call at 8s but not the cumulative total. With several active flights on slow upstreams the per-flight timeouts summed past the agent-runner's `SCRIPT_TIMEOUT_MS = 30s` execFile hard-kill (`container/agent-runner/src/index.ts`), killing the whole precheck — the observed ~34s being the 30s execFile timeout plus spawn/teardown.

`_run_cycle` now enforces an overall wall-clock budget (`_SCRIPT_KILL_BUDGET_SECONDS - _CYCLE_POLL_HEADROOM_SECONDS` — the 30s kill minus headroom for one in-flight poll plus interpreter startup/teardown). Before processing each flight it checks elapsed monotonic time; once the budget is reached it stops and defers the remaining flights to the next cycle, leaving their `last_polled_at` untouched so the cadence gate retries them — the same degraded-poll contract as the existing transient-transport branch. Deferred flights join the connection-risk exclusion set (`removed_upstream_ids | poll_failed_ids | deferred_ids`) because their snapshot wasn't verified this cycle. The budget clock is injected (`monotonic=time.monotonic`) so tests drive it deterministically without sleeping. Full per-flight concurrency (parallel byAir polls so total ≈ the slowest single call) would cut latency further but is a larger change, deferred as follow-up. Regression coverage: `test_wall_clock_budget_defers_remaining_slow_flights`, `test_connection_risk_excludes_budget_deferred_flights`.

### Fix — sync only the operator's own trips, not friends' (`jbaruch/nanoclaw-flight-assist#29`)

`sync_tripit._run_sync` called `byair.list_trips(status="active")` with no ownership argument, so the client default `ownership="all"` pulled friends' tracked trips into `active-flights.json`. The precheck then surfaced `[M]` wake events (delay, gate change, boarding) for flights the operator isn't on and can't act on — pure noise. The sync now requests `ownership="mine"`, so friends' flights never enter the index. The request-side filter is authoritative; the per-flight `ownership` field in the response is unreliable (defaults to `"mine"` when byAir omits it). Regression coverage: `test_sync_fetches_only_owned_trips`.

Deploy note: on the first sync after this ships, friends' flights already in `active-flights.json` reconcile as removed and would emit `tracked_flight_removed` (surfaced per SKILL.md). Prune those entries from the NAS state at deploy time to avoid a one-time "stopped tracking" burst. On-demand lookup of a friend's flight is tracked separately (expose byAir as an MCP tool to the agent).

### Fix — `build_lodging_ranges` no longer collapses repeat stays at one hotel (`jbaruch/nanoclaw-flight-assist#24`)

`check-travel-bookings.py:build_lodging_ranges` keyed check-in / check-out dates in dicts by hotel name alone, so a trip that bookended the same hotel (stay → other cities → same hotel again) overwrote the first stay and produced at most one range per hotel — under-reporting lodging coverage and surfacing false uncovered nights. Per-hotel events are now replayed in date order with a check-out closing the most recently opened stay (LIFO); same-hotel stays don't overlap, so the open stay is the one a check-out belongs to. This keeps a stray earlier check-out from matching a later check-in and an orphan earlier check-in from stealing a later stay's check-out (both would misreport coverage). Orphan check-outs form no range; unmatched check-ins keep the existing 1-day fallback. Unique-per-hotel trips (the common path) are unaffected. Regression coverage: `test_build_lodging_ranges_multiple_stays_same_hotel`, `test_build_lodging_ranges_same_hotel_extra_checkin_defaults_one_day`, `test_build_lodging_ranges_stray_earlier_checkout_not_consumed`, `test_build_lodging_ranges_orphan_earlier_checkin_not_stealing_later_stay`.

### Fix — don't flag same-day trips as missing hotel (`jbaruch/nanoclaw-admin#310`)

`check-travel-bookings.py`'s issue selector flagged any trip with transport and no lodging as "рейсы есть, отеля нет", including same-day round trips that need no overnight stay (Agentcon Miami: out + back on 2026-06-12, the return leg's arrival slipping to the next UTC day). The branch now treats a trip as needing no hotel only when the traveller is still in transit at the end of the trip window — the latest transport arrival within the trip reaches `trip_end`, as in a same-day round trip whose return slips past UTC midnight, or a red-eye that lands on the final day. When the latest arrival falls before `trip_end` the traveller has landed and is staying over. A missing hotel still surfaces in that case, including one-night single-leg trips and connecting outbounds, both of which `classify_trip`'s `has_future_transport` guard leaves with empty `uncovered_nights`. The signal is arrival-vs-`trip_end` rather than raw leg count. Two same-direction legs (a connecting outbound) are not a round trip, and leg count alone would misclassify them as one. Regression coverage: `test_classify_trip_same_day_round_trip_no_uncovered`, `test_main_same_day_trip_no_false_hotel_gap`, `test_main_one_night_single_leg_no_lodging_flagged`, `test_main_one_night_connecting_outbound_no_lodging_flagged`, and `test_main_multiday_single_transport_no_lodging_flagged`. The skill's output-contract doc was updated to match.

### Fix — bound `ByAirClient` per-call timeout in precheck to 8s (`jbaruch/nanoclaw-flight-assist#28`)

`precheck._run_cycle` now instantiates `ByAirClient.from_env(timeout=8.0)` instead of relying on the default 30s. A single slow byAir response previously raced the 30s `execFile` budget in `agent-runner` and surfaced as `precheck-error: execfile-error` — killing the whole cycle and producing a `task_run_logs` `status='error'` row that pinned `nanoclaw-admin` heartbeat into `system_health_issues` wake mode for 24h. With the per-call timeout below the outer budget, slow upstream calls fall through the existing transient-transport branch in `_run_cycle`, which skips the affected flight for one cycle (cadence gate retries it next tick) and lets other flights' polls complete.

Companion change in `ByAirClient._http_post`: `urlopen(..., timeout=X)` wraps connect-side socket timeouts as `urllib.error.URLError`, but a timeout during `response.read()` of the body propagates raw `TimeoutError` (since `socket.timeout` is aliased to `TimeoutError` in Python 3.10+). The body-read path is now wrapped to normalize `TimeoutError` into `URLError`, so `_run_cycle`'s transient-transport branch catches every transport timeout uniformly rather than letting body-read timeouts fall through to the outermost `precheck_exception` boundary and re-create the original cycle-kill symptom. Regression test: `test_body_read_timeout_surfaces_as_urlerror`.

### Fix — `_due_for_poll` forces a poll when `last_snapshot` is None (`jbaruch/nanoclaw-flight-assist#26`)

`_due_for_poll` now short-circuits to True when `last_snapshot is None`, so sync_tripit-seeded flights get polled on the next precheck tick instead of waiting up to a full cadence interval. Regression coverage: `test_seeded_state_with_no_snapshot_forces_poll`; two connection-risk tests updated to use a benign scheduled snapshot via the new `_scheduled_snapshot` helper.

### Added — `check-travel-bookings` migrated from `nanoclaw-admin` (`jbaruch/nanoclaw-admin#299`)

Per-chat travel concerns now consolidate under `nanoclaw-flight-assist`: flight notifications, time-to-leave, connection risk, arrival logistics, and now booking-gap detection. Coherent domain, single tile, single co-load for affected chats.

Migration is structural. `skills/check-travel-bookings/scripts/check-travel-bookings.py` and `skills/check-travel-bookings/scripts/build-travel-db.py` carry across with these edits, all from review feedback during PR #22:

- Non-behavioral cleanup: ruff-driven formatting (single → double quotes); B007/F841 `slug` / `item_count` dead-variable cleanup in `build-travel-db.py`; explicit `encoding="utf-8"` on file opens
- Hardening: `build-travel-db.py` now writes `travel-db.json` atomically (same-dir `.tmp` + `os.replace`) matching the `_atomic_write_json` pattern in `skills/flight-assist/state.py` — readers no longer see a half-written DB if the process is killed mid-write
- Diagnostic accuracy: `check-travel-bookings.py` adds `ensure_ascii=False` on the error-JSON path so operator-facing diagnostic messages keep their non-ASCII punctuation intact
- Stateful-artifacts contract — `travel-db.json` and `travel-booking-state.json` carry `schema_version: 1` per `coding-policy: stateful-artifacts`. New `state-schema.md` sibling documents owner / writers / readers / migration policy for both artifacts. The writer (`build-travel-db.py`) stamps `schema_version` on every output; the reader (`check-travel-bookings.py`) gates on it with explicit branches for legacy-implicit-v1, forward-incompatible (`> 1`), and non-int values. Snooze entries in `travel-booking-state.json` carry the same per-record field. Nine new tests pin the contract: DB at v1 / missing / forward / non-int; snooze entries at v1 / legacy / forward / non-dict-corrupt
- Test infra: `tests/conftest.py:_load` asserts `spec` and `spec.loader` are non-None so fixture-loading misconfigs fail at `_load` time with an actionable message instead of a deeper `AttributeError`

`skills/check-travel-bookings/SKILL.md` was restructured to follow `skill-authoring`'s execution-mode preamble + flat numbered step format (policy reviewer feedback); content is preserved, and Step 3 now instructs the agent to stamp `schema_version: 1` on snooze entries. The `gaps[]` example payload includes the `uncovered_nights` field the script actually emits.

Resolves the stateful-artifacts gap originally filed as #23 — that issue can close once this PR merges. The literal mount path `/home/node/.claude/skills/tessl__check-travel-bookings/scripts/<file>.py` used by `nightly-external-sync` Step 5 (`build-travel-db.py`) and `morning-brief` (`check-travel-bookings.py`) resolves to whichever tile owns the `check-travel-bookings` skill name — both consumers continue to work without code changes since the name doesn't change.

Tests follow: `tests/test_check_travel_bookings.py` and `tests/test_build_travel_db.py` migrated. New `tests/conftest.py` adds the two fixtures (`check_travel_bookings`, `build_travel_db`) ported from admin's conftest. Both scripts read/write `/workspace/group/travel-db.json` and `/workspace/group/travel-booking-state.json`; the writer chain (`refresh-travel-schedule.py` → `build-travel-db.py`) is unchanged from admin's perspective.

State plane note: existing `/workspace/group/travel-db.json` and `/workspace/group/travel-booking-state.json` carry across the migration as-is — they're group-scoped state files, not tile-shipped artifacts, so the deploy preserves operator-side snooze/resolve history.

### Review fixup (#21) — non-owner snapshot reader API + boundary handler at main()

OpenAI policy reviewer requested changes on two precondition violations in PR #21 (commit 5103c8f). Both addressed here:

1. **Module-level catch-all in `skills/sync-tripit/precheck.py` broadens the `error-handling` outer-boundary carve-out.** Removed the bootstrap try/except wrapping the cross-skill import. Path resolution + `sys.path` injection + the `state` import now live in a new `_load_flight_assist()` helper invoked from inside `main()`'s try block, so the sole catch-all sits at the outermost process boundary as the carve-out requires. The `_load_flight_assist` failure path is exercised by `test_main_bootstrap_failure_emits_safe_json`.

2. **Non-owner reader could invoke owner-side migrations.** `sync-tripit`'s precheck previously called `state.read_active_flights()` / `state.read_flight_state()`, both of which silently invoke `_migrate` and rewrite the file on `schema_version` mismatch — a violation of the single-owner contract in `coding-policy: stateful-artifacts`. Added a `migrate=True` kwarg to `_read_json_with_version` and exposed two dedicated non-owner reader entry points: `read_active_flights_snapshot()` and `read_flight_state_snapshot(flight_id)`. The snapshot readers treat any older `schema_version` as "no usable prior state" (return `[]` / `None`) and never write to disk; integrity failures (corrupt JSON, higher-than-current schema, missing required field at the current schema) still raise `StateError`. `precheck.py` now uses these snapshot readers. Owner-side `flight-assist` code paths are unchanged. `state-schema.md` documents the new reader contract.

Test coverage extended: gate tests now exercise the snapshot API; new `test_should_sync_does_not_migrate_old_active_flights` asserts the file's bytes + mtime are unchanged after a precheck run against a v1-schema state file; six new tests in `tests/test_state.py` cover the snapshot reader API (missing file, current payload, old-schema no-migrate, corruption, future-version, flight_id validation).

### Feat — adaptive scheduler for sync_tripit (new `sync-tripit` skill)

`sync_tripit.py` shipped at v0.1.0 with the docstring claim "Run cadence: daily at ~04:00 local" but never had a cadence-registry entry — the orchestrator never invoked it, so `active-flights.json` was never populated, and the existing 2-min flight-assist precheck loop fired into an empty state file every cycle. This is the orchestration half of the two-bug stack diagnosed live 2026-05-22 (byAir HTTP 400 was the transport half, addressed in PR #20).

The naïve fix would be `cadence: "0 4 * * *"` on flight-assist's existing frontmatter — but flight-assist already declares a `cadence:` for its 2-min precheck, and each SKILL.md gets one cadence-registry row. Beyond the structural constraint, daily-at-04:00 doesn't match the access pattern: day-of-travel changes (delays, gate moves, cancellations) need responsive polling, while between-travel-window periods don't justify any byAir traffic. Per the operator-stated requirement, the cadence should be ~5 minutes when there's a flight in the next 24 hours, idle otherwise.

New `skills/sync-tripit/` with `cadence: "*/5 * * * *"` + `script: "precheck.py"`. The precheck implements an adaptive gate: any tracked flight with `scheduled_dep_time` in the next 24h triggers a byAir round-trip; `active-flights.json` mtime older than 6h triggers a sync (catches newly-booked trips landing between travel windows); no state file yet triggers cold-start; otherwise emit `wake_agent: false` with no byAir call. When the gate passes, the precheck delegates to `flight-assist/sync_tripit.py` via subprocess and forwards its stdout — `sync_tripit.py` already emits the `{wake_agent, data}` wake-payload contract this script needs, so composition lives in one place.

Cross-skill import: `precheck.py` reads `state.py` and locates `sync_tripit.py` via the runtime mount path `/home/node/.claude/skills/tessl__flight-assist/` with a dev-clone-relative fallback (`../flight-assist`) for tests. Both skills ship from the same tile and are always co-deployed; if `flight-assist` is missing, the precheck raises `FileNotFoundError` at import time rather than silently failing.

Outer-boundary-process-contract handler in `main()` catches unexpected exceptions, emits safe-shape `{"wake_agent": false, "data": {"reason": "precheck_internal_error"}}`, exits 0 — the agent-runner reads non-zero exit OR invalid stdout JSON as `wake_agent: false`, which here would silently disable the entire flight-assist polling pipeline. Subprocess timeout (60s budget) surfaces as `sync_subprocess_timeout`; empty-stdout subprocess crashes surface as `sync_no_output`.

Adds `tile.json` entry for the new skill. Adds 13 mocked tests in `tests/test_sync_tripit_precheck.py`: gate correctness across cold-start / empty-recent / stale / imminent / out-of-window / past / multi-flight first-match / malformed-dep-time cases; subprocess delegation and forwarding; subprocess-timeout safe-shape conversion; outer-boundary exception handling. `tessl skill review` 87% (threshold 85). All pre-existing tests still pass.

### Fix — byAir MCP client `Accept` header missing `text/event-stream` (HTTP 400 on every call)

`byair_client.py` set `Accept: application/json` on both `_initialize` and `_tools_call` outbound headers. The byAir MCP streamable-HTTP endpoint rejects with `HTTP 400 — "Accept must contain both 'application/json' and 'text/event-stream'"` because the MCP streamable-HTTP spec requires clients to advertise support for both response shapes (servers may stream tool responses via SSE). The `_SessionExpired` retry path doesn't engage on `initialize` because `self._session_id is None` at that point, so the original 400 propagated and the client could not complete the handshake. Result: every byAir call from a fresh process failed at the handshake, and `sync_tripit.py` could not populate `active-flights.json`. Combined with the orchestration gap leaving `sync_tripit` unscheduled, the precheck loop fired every 2 min, read an empty state file, and emitted `wake_agent: false` on every cycle — the skill was "installed but deaf" on flight days.

Two-line fix: `Accept: "application/json"` → `Accept: "application/json, text/event-stream"` at both header sites. Verified live against the production byAir endpoint 2026-05-22 — `initialize` returned HTTP 200 with a valid `mcp-session-id`. Q-value forms (`application/json; q=1.0, text/event-stream; q=0.1`) are NOT accepted by the byAir server — it does substring matching after splitting on `,` and the parameter-suffixed entries don't match the bare-token check; the verified-working form is the plain comma-separated list.

Defensive `Content-Type` guard added in `_http_post`: since the client now advertises `text/event-stream`, a server could pick SSE for some response. We don't parse SSE here (the operations this client uses — `initialize`, `notifications/initialized`, non-streaming tool calls — all return JSON in practice). The guard raises a clear `ByAirError("unsupported_response_shape", ...)` if Content-Type comes back as `text/event-stream`, rather than letting `json.loads` fail with a cryptic decoder error on `event:` / `data:` SSE prefixes.

Adds two regression tests in `tests/test_byair_client.py`: one asserts the outbound `Accept` header includes both content types on initialize, notification, and tool-call requests (via mocked `urlopen` per `coding-policy: testing-standards`); the other asserts the Content-Type guard raises the actionable error on a mocked SSE response. 15/15 byair_client tests pass.

The orchestration gap (no cadence-registry entry, no scheduled-task row for `sync_tripit` itself) lands separately — fixing the transport doesn't help if nothing ever invokes the feeder.

### Fix — install wake task + origin-resolution ladder (`jbaruch/nanoclaw-flight-assist#17`, `#18`)

Two coupled bugs that left the skill installed-but-silent on a mobile traveller's flight days.

**#17** — `precheck.py` never fired. The scheduled-task row that runs the precheck every 2 minutes is provisioned via the host orchestrator's cadence-registry (host repo `src/cadence-registry.ts`), which reads `cadence:` + `script:` from each installed skill's SKILL.md frontmatter on container spawn. `flight-assist/SKILL.md` carried no such declaration, so the registry walked past it and never created the row. Verified live 2026-05-20: no `scheduled_tasks` row matched any flight-assist / byair prompt despite the skill being fully installed. Add `cadence: "*/2 * * * *"` + `script: "precheck.py"` to the frontmatter; the cadence-registry's rebuild on the next container respawn provisions the row. The existing per-flight cadence ladder inside `_interval_for` still gates byAir calls per-flight, so the 2-min wake floor doesn't translate into 2-min byAir traffic.

**#18** — `time_to_leave` resolved origin exclusively from `config.json:home_address`. A constant traveller (rarely at home on flight days) got either silent failure (no home base set) or structurally wrong notifications (home base set but user is 5000 km away). Add an origin-resolution ladder in `precheck._resolve_time_to_leave_origin`: (1) fresh `/workspace/state/flight-assist/current-location.json` snapshot (≤ 30 min old, formatted as `"lat,lng"` for Distance Matrix) → (2) `home_address` fallback → (3) `None` (skip the maps query when neither is available). The snapshot file is **host-orchestrator-owned** — flight-assist is a non-owner reader per `coding-policy: stateful-artifacts`, validates the documented shape, and returns `None` on any mismatch instead of raising. Without the orchestrator-side write the precheck behaves exactly as today (home_address-only); once the orchestrator's location-write companion lands the live ladder takes over. State-schema doc updated to describe the new file shape and reader contract.

### Skills — added

- **`skills/flight-assist/connection_risk.py`** — V1.1 cross-flight connection-risk detector (capability 4 of the V1 spec, previously deferred). Pure-function detector that groups on-disk per-flight state by `trip_id`, sorts each group by `scheduled_dep_time`, and walks consecutive (leg-1, leg-2) pairs where `arr_airport_id(leg-1) == dep_airport_id(leg-2)`. Emits `connection_at_risk` events when the projected transfer window (`scheduled_dep(leg-2) - projected_arr(leg-1)`, taking leg-1's live `arr_time` when populated and `scheduled_arr_time` as fallback) is below `min_transfer_minutes` (config-overridable, default 45). Suppression rules: leg-1 status in `{landed, cancelled, diverted}`, leg-1 scheduled departure > 24h away, `connection_at_risk_fired` already True on leg-2's marker. The event is keyed on leg-2's `flight_id` so the once-per-flight marker survives leg-1 landing. Closes #14.

- **`skills/flight-assist/precheck.py`** — post-loop pass `_check_connection_risks` runs after every cycle's per-flight processing, reads the now-up-to-date flight states, calls `detect_connection_risks`, and flips `connection_at_risk_fired` on each fired leg-2 record before emitting the event. `_initial_phase_markers` includes the new marker key.

- **`skills/flight-assist/SKILL.md`** — composition table gains a `connection_at_risk` row that renders the tight-connection notification. Description triggers extended with "connection at risk" / "tight connection alert" so the runtime matches the new intent.

- **`skills/flight-assist/references/event-payloads.md`** — new "Cross-flight" section documenting the `connection_at_risk` shape and suppression rules.

- **`skills/flight-assist/sync_tripit.py`** — `_initial_state` includes `connection_at_risk_fired: false` on the new-flight phase_markers dict.

### State schema — v1 → v2

- **`skills/flight-assist/state.py`** — `STATE_SCHEMA_VERSION` bumped to `2`. Owner-side migration (`_migrate`) handles the v1 → v2 upgrade: adds `connection_at_risk_fired: false` to per-flight `phase_markers`, rewrites at v2. Config and active-flights files have no shape change at v2; they receive a schema_version bump only via the same migration code path on first read. Per `coding-policy: stateful-artifacts` "Migration Policy", only the owner skill migrates — reader skills from other tiles continue to get `StateError` on mismatched version. Strict reader contract preserved: missing/wrong-type `schema_version`, schema_version higher than current, and corrupt JSON still raise `StateError` with actionable repair guidance.

- **`skills/flight-assist/state.py`** — `_PHASE_MARKER_KEYS` extended with `connection_at_risk_fired`; the read+write validators reject phase_markers dicts missing the new key or carrying it as a non-bool. `_CONFIG_OPTIONAL_FIELDS` extended with `min_transfer_minutes: int` (with explicit bool-rejection on int fields to match the rest of the validator family).

- **`skills/flight-assist/state-schema.md`** — documents v2 shape, the new `connection_at_risk_fired` marker (carried on leg-2 so it survives leg-1 landing), and the new optional `min_transfer_minutes` config field. The Migration Policy section names the v1 → v2 migration explicitly and reaffirms the owner-only migration discipline.

### Skills — added (V1)

- **`skills/flight-assist/sync_tripit.py`** — daily reconciliation of the active-flights index against byAir's `list_trips`. Reads upstream, diffs against the on-disk `active-flights.json`, writes initial state records for added flights, deletes state for removed flights, emits the same `{wake_agent: bool, data: {events: [...]}}` payload shape as `precheck.py` — sync adds use `reason: "tracked_flight_added"` and sync removes use `reason: "tracked_flight_removed"` so SKILL.md Step 3's composition table is the single consumer contract. Removed-flight events capture `code` + scheduled times BEFORE state deletion so the agent has the metadata it needs to render notifications. Same outer-boundary-process-contract carve-out as `precheck.py`. Exports `initialize_flight_from_byair()` for the precheck to call when it encounters a flight_id not yet on the index. stdlib-only.

- **`skills/flight-assist/SKILL.md`** — full V1 action-router SKILL.md. Three actions: `Diagnose env` (preserved from v0.1.0), `Set home base` (records `home_address` to config via `state.write_config`), and `Compose wake event notification` (per-event-type composition table covering all 10 documented wake reasons — `cancelled`, `diverted`, `gate_change`, `delay`, `inbound_delay_predicted`, `boarding_started`, `carousel_revealed`, `day_before`, `time_to_leave`, `arrival_logistics`, `removed_upstream`). Multi-event merging rule (one notification per flight per cycle, ordered by urgency). References `references/event-payloads.md` for the full event-shape contract. Skill review: 90% (Description 90, Content 85).

- **`skills/flight-assist/references/event-payloads.md`** — reference document for the precheck wake-event payload shapes. One section per `reason` with the JSON shape + when it fires + composition discipline.

- **`skills/flight-assist/precheck.py`** — the scheduler-invoked entry point that orchestrates byair_client + maps_client + state + wake_rules + phase_markers. Reads `active-flights.json`, cadence-gates each flight (per `state-schema.md`'s `last_polled_at` discipline), fetches new snapshots from byAir for due flights, runs delta detection (`wake_rules`) + time-based gates (`phase_markers`), persists updated state, and emits a single-line JSON payload on stdout: `{"wake_agent": <bool>, "data": {"events": [...]}}` per `coding-policy: script-delegation` "Precheck Gating". Uses the outer-boundary-process-contract carve-out: any unhandled exception is caught at the script boundary so the scheduler always sees safe-shape JSON + exit 0 (a bare programming bug would otherwise silently disable the wake contract). `_run_cycle()` takes `now_utc` as a parameter so tests pin the clock without monkey-patching `datetime`. Queries `maps_client.travel_time()` only for flights within the 6-hour time-to-leave window — preserves the Distance Matrix per-query budget.

- **`skills/flight-assist/phase_markers.py`** — time-based wake-gate functions for the precheck. Three once-per-flight events driven by wall-clock time alone (vs `wake_rules.py`'s delta detection): `day_before` (T-24h, capability 2's sanity check), `time_to_leave` (traffic-aware leave-by, capability 1), `arrival_logistics` (T-arr−15min, capability 6). Each function takes `phase_markers` (the per-flight state dict that tracks once-fired flags) plus a synthetic `now_utc` for deterministic testing. Returns `(should_fire, event_dict)`. `time_to_leave` consumes `travel_time_seconds` from `maps_client.travel_time().in_traffic_seconds`; defers when `None` (the caller decides when to query maps per the cadence-ladder budget). Pure functions, no I/O, no state mutation.

- **`skills/flight-assist/wake_rules.py`** — pure-function delta-event detector for the precheck. Takes `(prev_snapshot, new_snapshot)`, returns a list of wake events `[{"reason": "...", ...}, ...]`. Event types: `cancelled`, `diverted`, `gate_change` (with side + from + to), `delay` (with delay_minutes + new_dep_time, threshold ≥15 min), `inbound_delay_predicted` (threshold ≥20 min, dedupe within 5 min vs prior magnitude), `boarding_started`, `carousel_revealed` (with baggage claim). First-cycle behavior: `cancelled` / `diverted` fire from a None prev (the state itself is news), other rules require a prior snapshot. RFC3339 timestamps compared in UTC so DST/offset shifts don't false-positive. No I/O, no logging, no state mutation — per `coding-policy: script-delegation` (deterministic logic stays in scripts).

- **`skills/flight-assist/state.py` + `state-schema.md`** — per-flight state file read/write under `/workspace/state/flight-assist/` (configurable via `FLIGHT_ASSIST_STATE_DIR` env var for tests). Atomic writes (write-to-tmp + `os.replace`) so a kill mid-write doesn't leave a half-written file. Three file types: `config.json` (home_address from /setup), `active-flights.json` (index of tracked flight_ids), `flight-<flight_id>.json` (per-flight record with snapshot, phase_markers, last_polled_at). All carry `schema_version: 1`. `state-schema.md` documents the full per-record contract per `coding-policy: stateful-artifacts`. Owner skill: `flight-assist` — when a future schema bump ships, the owner skill adds migration branches that upgrade-and-rewrite. Read-side validation is strict: `schema_version` must equal `STATE_SCHEMA_VERSION` and be a plain `int` (no `bool`, no string); `flight_ids` must be a list of plain ints (no silent coercion). Mismatches raise `StateError` with actionable repair messages per `coding-policy: error-handling`. stdlib-only (`json` + `os` + `pathlib`).

- **`skills/flight-assist/maps_client.py`** — Google Maps Distance Matrix client for traffic-aware travel-time queries. Used by `phase_markers.py` (forthcoming) to compute the "leave by" deadline for the time-to-leave capability. stdlib-only (`urllib.request` + `urllib.parse` + `json`). Public API: `MapsClient.from_env()` + `travel_time(origin, destination) → TravelTime` (frozen dataclass with `duration_seconds`, `in_traffic_seconds`, `traffic_factor`, `distance_meters`, `origin_resolved`, `destination_resolved`). Uses `departure_time=now` + `traffic_model=best_guess` so every request includes a current-traffic estimate when the API returns one. `MapsError(status, message)` wraps non-OK top-level and per-element statuses (`NOT_FOUND`, `ZERO_RESULTS`, `OVER_QUERY_LIMIT`, `REQUEST_DENIED`, `MALFORMED_RESPONSE`); HTTP transport errors propagate as `urllib.error.HTTPError`.

- **`skills/flight-assist/byair_client.py`** — Python HTTP client wrapping the byAir streamable-HTTP MCP endpoint as a JSON-RPC API. Used by the (forthcoming) precheck script, not registered as a Claude MCP tool inside the agent container — the precheck filters the ~13KB raw byAir response down to a ~1KB operational slice before any state write, so the agent never sees the full payload. stdlib-only (`urllib.request` + `json`) per `coding-policy: dependency-management`. Public API: `ByAirClient.from_env()` + `get_flight()` / `list_trips()` / `get_flight_notifications()`. Wraps `isError: true` responses as `ByAirError(error_type, message)`; HTTP errors propagate as `urllib.error.HTTPError`. Sessions are managed lazily with one transparent re-init + retry on session-invalid 4xx; a second failure surfaces the underlying HTTPError so the caller sees the real transport error.

### Rules

- **Closed-loop carve-out claimed for `jbaruch/coding-policy: plugin-evals`** (2026-05-18). This tile is part of the `jbaruch/nanoclaw-*` plugin fleet — a fully-automated agent loop satisfying all three preconditions of the rule's "Narrow exception for closed-loop automated systems with no human eval-result consumption" clause: (1) no human reviews eval output for this tile in any form (no eval scores, no lift deltas, no scenario-by-scenario diffs, no regression alerts); (2) no automated gate consumes eval results (no `evals.yml` workflow, no publish-tile eval step, no downstream dashboard or paging route); (3) the owner accepts that re-introducing any consumption of eval results later — whether human review OR automated gating — requires re-introducing evals first under the standard requirement. Matches the carve-out previously claimed by `jbaruch/nanoclaw-admin` on 2026-05-09 and inherited by every `jbaruch/nanoclaw-*` tile thereafter. No `evals/` directory ships in this tile.

### Initial scaffold

- **`tile.json`** — declares `jbaruch/nanoclaw-flight-assist` 0.1.0, public, with one rule (`flight-data-locality`) and one skill (`flight-assist`)

- **`rules/flight-data-locality.md`** — byAir is the single source of truth for flight data; second flight-data upstreams are forbidden by default. The motivation behind the rule: byAir pre-computes phase logic (`computed_status`, `computed_phase_progress`, `computed_phase_risk`, `computed_phase_overdue`) and inbound-aircraft prediction (`inbound.predicted_delay`). Mixing a raw-status API would force a translation layer between two semantically-different models. The byAir Pro subscription covers every operational field this tile needs, so a second upstream would add a separate budget, a separate key, and a separate rate-limit posture for marginal data. When byAir reports "boarding" and a second API reports "scheduled", the reconciliation question has no clean answer — one upstream, one truth. Eval of byAir's MCP on 2026-05-17 confirmed all six target capabilities are addressable from byAir alone (with maps/traffic as a separate axis).

- **`skills/flight-assist/SKILL.md`** — minimal sequential-workflow skill with one step: run `check-env.py`, report missing credentials with actionable fix instructions. Will evolve into an action router as polling, state, and event composition land in subsequent PRs.

- **`skills/flight-assist/scripts/check-env.py`** — env-presence check for `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`. Emits single-line JSON; exit 0 always (info-only, not a gate).

- **`.env.example`** — documents required environment variables per `coding-policy: no-secrets`, including the deep link to the GitHub Actions secrets configuration page so a new maintainer reaches the settings page in one click.

- **CI workflows** — `test.yml` runs ruff + pytest on every PR; `publish-tile.yml` uses `jbaruch/coding-policy/.github/actions/skill-review@<sha>` (the canonical changed-skills loop) before `tessl tile lint` and `tesslio/patch-version-publish` on `main`.

- **`pyproject.toml` + `requirements-dev.txt`** — pytest 8.3.4 + ruff 0.7.4, ruff scoped to `tests/` and `skills/` per `coding-policy: code-formatting` (every shipped Python file goes through lint + format check; new skill scripts under `skills/<name>/scripts/` inherit coverage automatically).

- **MIT license** — matches the public `nanoclaw-*` fleet.

- **`.tileignore`** — excludes repo-only files (CI, tests, build artifacts, dev-time tessl-install scaffolding) from the published Tessl tile per `coding-policy: context-artifacts`.
