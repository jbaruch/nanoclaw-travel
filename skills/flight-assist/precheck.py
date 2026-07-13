#!/usr/bin/env python3
"""Flight-assist precheck script — the scheduler-invoked entry point.

The scheduled task runs this script every ~2 minutes. It reads the
active-flights index, cadence-gates per flight, queries byAir for
flights whose poll-interval has elapsed, runs wake_rules + phase_markers
on each, writes updated state, and emits a single-line JSON payload
on stdout:

    {"wake_agent": <bool>, "data": {"events": [...]}}

When `wake_agent` is true, the scheduler wakes the agent with the
events list as context; the agent composes user notifications.
When false, zero LLM tokens spent. Per `coding-policy:
script-delegation` "Precheck Gating".

The cadence ladder (per `state-schema.md`'s `last_polled_at`
discipline):

    scheduled, T > 6h:                    30 min
    scheduled, 2h < T ≤ 6h:               10 min
    check_in_open / boarding:             2 min
    departed / en_route, T-arr > 30 min:  5 min
    en_route, T-arr ≤ 30 min:             2 min  (catch carousel reveal)
    landed (until acknowledged):          5 min
    cancelled / diverted:                 60 min (visibility only)

The script is the OUTER PROCESS BOUNDARY of the scheduled-task
contract — the scheduler reads non-zero exit OR malformed stdout as
"skip waking the agent this cycle". An unhandled exception there
silently disables the contract per `coding-policy: error-handling`
outer-boundary-process-contract carve-out.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.error
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BUNDLE_DIR))

# trip_origin ships in the co-located travel-core bundle (same plugin). Resolve
# it at the runtime mount, falling back to the dev-clone sibling so the import
# works both on the NAS and in CI — same cross-bundle pattern as drive-planner.
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
sys.path.insert(0, str(_TRAVEL_CORE))

from boarding_lead import resolve_boarding_lead_minutes  # noqa: E402
from byair_client import ByAirClient, ByAirError  # noqa: E402
from connection_risk import (  # noqa: E402
    DEFAULT_MIN_TRANSFER_MINUTES,
    detect_connection_risks,
)
from maps_client import MapsClient, MapsError  # noqa: E402
from phase_markers import (  # noqa: E402
    check_arrival_logistics,
    check_day_before,
    check_gate_assignment,
    check_time_to_leave,
    gate_assignment_window_open,
    is_boarding_or_gone,
)
from state import (  # noqa: E402
    read_active_flights,
    read_config,
    read_flight_state,
    resolve_live_origin,
    write_flight_state,
)
from trip_origin import resolve_effective_home  # noqa: E402
from trip_window import evaluate_trip_window  # noqa: E402
from wake_rules import detect_wake_events  # noqa: E402

# Cadence ladder (minutes). Keyed by `computed_status`; the
# scheduled / en_route bins split further on time-to-departure or
# time-to-arrival inside `_minutes_until_next_poll`.
_BASE_CADENCE_MINUTES = {
    "scheduled": 30,
    "check_in_open": 2,
    "boarding": 2,
    "departed": 5,
    "en_route": 5,
    "landed": 5,
    "cancelled": 60,
    "diverted": 60,
}

# Poll horizon: flights whose scheduled departure is more than this many
# hours away are not polled. sync_tripit keeps them in the index, but they
# cost no byAir call until they approach departure — shrinking the per-cycle
# poll batch at the source (the dominant pile-up behind #36). 24h clips
# nothing: the earliest precheck event is `day_before` at T-24h, and
# `connection_risk` already gates leg-1 on its own 24h lookahead. Per #38.
_POLL_HORIZON_HOURS = 24

# Window for querying maps_client for travel time. Past this window
# the time-to-leave marker has either fired or doesn't matter.
_TIME_TO_LEAVE_QUERY_WINDOW_HOURS = 6

# Per-call timeout for the byAir HTTP client inside `_run_cycle`. The outer
# `execFile` budget in agent-runner is 30s; a single slow byAir response at
# the client's default 30s timeout would race that budget and surface as
# `precheck-error: execfile-error`. 8s allows several slow calls to fail-fast
# into the URLError transient-transport branch before the outer budget runs
# out, converting a whole-cycle kill into per-flight retries. Per #28.
_BYAIR_CALL_TIMEOUT_SECONDS = 8.0

# Per-call timeout for the Maps Distance-Matrix client inside `_process_flight`.
# Same rationale as byAir's bound (#28): the MapsClient default is 10s, but the
# poll-loop headroom must reserve for it, so pin it to the byAir bound and derive
# the headroom from both. Without this, a single slow Maps query stacked on a
# byAir poll overran the 30s hard-kill — see the headroom note below.
_MAPS_CALL_TIMEOUT_SECONDS = 8.0

# Wall-clock budget for the whole poll loop. The agent-runner hard-kills the
# precheck process at SCRIPT_TIMEOUT_MS = 30s (container/agent-runner/src/index.ts)
# and surfaces the kill as `execfile-error`. Polls run sequentially, so several
# active flights on slow upstreams each pay up to _BYAIR_CALL_TIMEOUT_SECONDS and
# their sum can exceed 30s — killing the whole cycle. #28 bounded each call but
# not the cumulative total. `_run_cycle` stops starting new polls once this
# budget elapses and defers the remaining flights to the next cycle. The budget
# is the kill timeout minus headroom for the worst-case work a single
# already-started flight can still do plus interpreter startup/teardown, so a
# poll started just under the budget still returns before the kill. Per #36.
#
# The headroom MUST cover one byAir poll PLUS one Maps travel-time query — both
# happen inside a single `_process_flight`. The earlier 10s headroom only
# covered the byAir poll, so a flight started just under the budget ran byAir
# (8s) + Maps (10s default) ≈ 18s and overran the kill, surfacing as
# `execfile-error` (jbaruch/nanoclaw#562 traced the heartbeat wake-storm partly
# to these crashes). Deriving the headroom from the two call timeouts keeps it
# correct if either changes.
_SCRIPT_KILL_BUDGET_SECONDS = 30.0
_INTERPRETER_TEARDOWN_HEADROOM_SECONDS = 4.0
_CYCLE_POLL_HEADROOM_SECONDS = (
    _BYAIR_CALL_TIMEOUT_SECONDS
    + _MAPS_CALL_TIMEOUT_SECONDS
    + _INTERPRETER_TEARDOWN_HEADROOM_SECONDS
)
_CYCLE_WALL_CLOCK_BUDGET_SECONDS = _SCRIPT_KILL_BUDGET_SECONDS - _CYCLE_POLL_HEADROOM_SECONDS


def main() -> int:
    # outer-boundary-process-contract: this script is the scheduled-task's
    # outermost process boundary. The scheduler reads non-zero exit OR
    # invalid stdout as "skip waking the agent". A bare programming bug
    # bubbling out would silently disable the wake contract for every
    # subsequent run, so the outermost catch emits a safe-shape JSON +
    # writes the traceback to stderr per error-handling.md's carve-out.
    try:
        now_utc = datetime.now(timezone.utc)
        # Defense-in-depth trip-window gate (#147). The host pre-spawn gate
        # (jbaruch/nanoclaw#754) should already keep the container from spawning
        # off-window; if one spawns anyway, bail here — before any byAir call —
        # reading the SAME travel-db.json with the SAME formula so the two layers
        # agree. Fail-open on a corrupt file never blinds an active trip.
        window = evaluate_trip_window(now_utc=now_utc)
        if not window.in_window:
            _emit(
                {
                    "wake_agent": False,
                    "data": {"reason": "outside_trip_window", "detail": window.reason},
                }
            )
            return 0
        events = _run_cycle(now_utc=now_utc)
        _emit({"wake_agent": bool(events), "data": {"events": events}})
        return 0
    except Exception:  # noqa: BLE001 — outer-boundary-process-contract
        traceback.print_exc(file=sys.stderr)
        _emit({"wake_agent": False, "data": {"error": "precheck_exception"}})
        return 0  # exit 0 with safe-shape JSON so the scheduler reads "no wake"


def _emit(payload: dict) -> None:
    """Write the precheck contract JSON to stdout (single line)."""
    print(json.dumps(payload, separators=(",", ":")))


def _run_cycle(
    *,
    now_utc: datetime,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Execute one precheck cycle, return aggregated wake events.

    `now_utc` is injected so tests can pin the clock without monkey-patching
    the `datetime` module (which breaks `fromisoformat()` deeper in the
    module). Production callers pass `datetime.now(timezone.utc)`.

    `monotonic` is injected so tests can drive the wall-clock budget
    deterministically without sleeping. Production callers use
    `time.monotonic`. It measures elapsed time for the poll-loop budget;
    `now_utc` is logical (cadence) time and must not be reused here because
    a test that pins `now_utc` would otherwise freeze the budget clock too.
    """
    active_flight_ids = read_active_flights()
    config = read_config() or {}
    # Trip-aware (#122): "home" for this cycle is the static residence
    # off-trip, but the current lodging while a TripIt trip is active —
    # routing a time-to-leave from a residence an ocean away is worse than
    # not routing at all. None mid-trip (no lodging yet) disables routing
    # via the existing no-home handling.
    home_address = resolve_effective_home(config.get("home_address"), now=now_utc)
    min_transfer_minutes = _resolve_min_transfer_minutes(config)

    # Resolve the time-to-leave origin once per cycle so every flight
    # in this cycle queries Distance Matrix against the same snapshot.
    # The host-orchestrator-owned `current-location.json` could be
    # rewritten mid-cycle by a concurrent location update; reading it
    # once per `_process_flight` would let two flights in the same
    # cycle disagree on where the user is, which is incoherent. Per
    # Copilot review on `jbaruch/nanoclaw-flight-assist#19`.
    cycle_origin = _resolve_time_to_leave_origin(home_address=home_address, now_utc=now_utc)

    byair = ByAirClient.from_env(timeout=_BYAIR_CALL_TIMEOUT_SECONDS)
    maps = _maybe_maps_client()  # None when GOOGLE_MAPS_API_KEY unset

    aggregated_events: list[dict] = []
    # Per `coding-policy: stateful-artifacts` — on-disk state is a
    # last-seen snapshot, not ground truth. Three distinct exclusion
    # categories feed the cross-flight pass's eligibility decision:
    #
    # - `removed_upstream_ids`: flights confirmed gone (byAir 404).
    #   Their stale snapshot must never produce a derived alert because
    #   we KNOW it's a lie.
    # - `poll_failed_ids`: flights whose poll was attempted this cycle
    #   and failed (non-404 byAir error, URLError transport failure).
    #   We can't verify the snapshot is current, and the rule says
    #   "before acting on a recalled value, verify against the live
    #   source" — failed verification means we don't act.
    # - `deferred_ids`: flights skipped this cycle because the wall-clock
    #   budget elapsed before we reached them. Unverified this cycle, same
    #   as `poll_failed_ids` — exclude them so a stale snapshot doesn't
    #   feed a derived alert. They retry next cycle.
    #
    # Flights NOT due to poll this cycle keep their cadence-bounded
    # freshness contract and remain eligible — their snapshot is within
    # the cadence ladder's staleness budget by construction.
    removed_upstream_ids: set[int] = set()
    poll_failed_ids: set[int] = set()
    deferred_ids: set[int] = set()
    poll_deadline = monotonic() + _CYCLE_WALL_CLOCK_BUDGET_SECONDS
    for index, flight_id in enumerate(active_flight_ids):
        if monotonic() >= poll_deadline:
            # Budget elapsed. Defer this flight and every flight after it
            # to the next cycle rather than risk the agent-runner's 30s
            # hard-kill mid-poll. Their `last_polled_at` is left untouched
            # (we never call `_process_flight`), so the cadence gate retries
            # them next tick — the same degraded-poll contract as the
            # transient-transport branch below. Per #36.
            remaining = active_flight_ids[index:]
            deferred_ids.update(remaining)
            print(
                f"flight-assist precheck: wall-clock budget "
                f"({_CYCLE_WALL_CLOCK_BUDGET_SECONDS:.0f}s) reached after "
                f"{index} of {len(active_flight_ids)} flights; deferring "
                f"{len(remaining)} to next cycle",
                file=sys.stderr,
            )
            break
        try:
            flight_events = _process_flight(
                flight_id=flight_id,
                now_utc=now_utc,
                byair=byair,
                maps=maps,
                time_to_leave_origin=cycle_origin,
            )
        except ByAirError as byair_err:
            # 404 on the byAir side means the flight is no longer
            # tracked upstream; surface as a removed_upstream event.
            if byair_err.error_type == "not_found":
                aggregated_events.append(
                    {"flight_id": flight_id, "event": {"reason": "removed_upstream"}}
                )
                removed_upstream_ids.add(flight_id)
                continue
            # Other byAir errors: log to stderr, skip this flight this
            # cycle (don't update last_polled_at so it retries next
            # cycle). The cycle attempted verification and failed, so
            # the flight is excluded from cross-flight derivations.
            print(
                f"flight-assist precheck: byair error for flight {flight_id}: {byair_err}",
                file=sys.stderr,
            )
            poll_failed_ids.add(flight_id)
            continue
        except urllib.error.URLError as transport_err:
            # Transient transport failure (network, DNS, byAir down).
            # Degrade this flight's poll for this cycle instead of
            # collapsing the whole precheck via the outer catch — other
            # flights' polls still get a chance. last_polled_at is not
            # updated, so the cadence-gate fires for this flight next
            # cycle. Per `coding-policy: error-handling` "Specific
            # Exceptions" + "Graceful Fallback". This cycle attempted
            # verification and failed, so the flight is excluded from
            # cross-flight derivations per `coding-policy:
            # stateful-artifacts` (verify before recall).
            print(
                f"flight-assist precheck: transport error for flight {flight_id}: {transport_err}",
                file=sys.stderr,
            )
            poll_failed_ids.add(flight_id)
            continue
        for event in flight_events:
            aggregated_events.append({"flight_id": flight_id, "event": event})

    # Connection-risk pass: walks the now-up-to-date on-disk state to
    # group flights by trip_id and emit cross-flight risk events. Excludes
    # removed-upstream flights (snapshot known to lie), poll-failed flights
    # (snapshot unverified this cycle), and budget-deferred flights (not
    # polled this cycle) per stateful-artifacts.
    #
    # Horizon-skipped flights (departure > _POLL_HORIZON_HOURS) are NOT
    # excluded, by design. detect_connection_risks reads only leg-2's seeded
    # scheduled_dep_time / dep_airport_id / markers — never its last_snapshot
    # (which polling never refreshes anyway; _build_flight_state preserves the
    # sync-seeded scheduled times) — and gates leg-1 on its own 24h lookahead
    # using leg-1's live, freshly-polled snapshot. So the firing decision never
    # rests on a skipped flight's unverified snapshot, and a tight connection
    # where leg-1 is imminent but leg-2 sits just past the horizon still fires.
    excluded_ids = removed_upstream_ids | poll_failed_ids | deferred_ids
    risk_candidate_ids = [fid for fid in active_flight_ids if fid not in excluded_ids]
    aggregated_events.extend(
        _check_connection_risks(
            active_flight_ids=risk_candidate_ids,
            now_utc=now_utc,
            min_transfer_minutes=min_transfer_minutes,
        )
    )
    return aggregated_events


def _check_connection_risks(
    *,
    active_flight_ids: list[int],
    now_utc: datetime,
    min_transfer_minutes: int,
) -> list[dict]:
    """Run the cross-flight connection-risk pass and persist fired markers.

    Returns the `{flight_id, event}`-shaped list ready to merge into the
    precheck's aggregated event output. For each fired event, this
    function flips the leg-2 flight's `connection_at_risk_fired` marker
    in state so subsequent cycles don't re-fire.
    """
    flight_states: list[dict] = []
    for fid in active_flight_ids:
        state = read_flight_state(fid)
        if state is not None:
            flight_states.append(state)
    risks = detect_connection_risks(
        flight_states=flight_states,
        now_utc=now_utc,
        min_transfer_minutes=min_transfer_minutes,
    )
    states_by_id = {s["flight_id"]: s for s in flight_states}
    emitted: list[dict] = []
    for leg2_flight_id, event in risks:
        leg2_state = states_by_id.get(leg2_flight_id)
        if leg2_state is None:
            continue
        leg2_state["phase_markers"]["connection_at_risk_fired"] = True
        write_flight_state(leg2_state)
        emitted.append({"flight_id": leg2_flight_id, "event": event})
    return emitted


def _resolve_min_transfer_minutes(config: dict) -> int:
    """Return the validated `min_transfer_minutes` from config, or the default.

    The on-disk config can be hand-edited; `write_config` rejects bad
    types and negative values but a manually-edited file with
    `"min_transfer_minutes": "45"` or `True` would slip past the writer.
    Coerce defensively here so a corrupt config doesn't propagate into
    `detect_connection_risks` and surface as the `ValueError` the public
    API raises on invalid input — which the outer-boundary catch would
    then suppress for the entire cycle. Falling back to the default keeps
    the cycle running while the stderr diagnostic flags the bad config
    for the operator.
    """
    value = config.get("min_transfer_minutes")
    if value is None:
        return DEFAULT_MIN_TRANSFER_MINUTES
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        print(
            f"flight-assist precheck: config.json:min_transfer_minutes is "
            f"{type(value).__name__} {value!r}, expected non-negative int — "
            f"falling back to {DEFAULT_MIN_TRANSFER_MINUTES}",
            file=sys.stderr,
        )
        return DEFAULT_MIN_TRANSFER_MINUTES
    return value


def _maybe_maps_client() -> MapsClient | None:
    """Return a MapsClient if GOOGLE_MAPS_API_KEY is set, else None.

    Time-to-leave is the only consumer; if no key is configured the
    precheck still runs but won't fire that one event type.
    """
    if not os.environ.get("GOOGLE_MAPS_API_KEY"):
        return None
    return MapsClient.from_env(timeout=_MAPS_CALL_TIMEOUT_SECONDS)


def _process_flight(
    *,
    flight_id: int,
    now_utc: datetime,
    byair: ByAirClient,
    maps: MapsClient | None,
    time_to_leave_origin: str | None,
) -> list[dict]:
    """Process a single flight: cadence-gate, fetch, diff, emit events.

    `time_to_leave_origin` is resolved once per cycle by `_run_cycle`
    and passed in here, so every flight processed in the same cycle
    agrees on the user's location even when the host-orchestrator-owned
    `current-location.json` is rewritten mid-cycle.
    """
    prior_state = read_flight_state(flight_id)
    prior_snapshot = prior_state.get("last_snapshot") if prior_state else None
    phase_markers = prior_state.get("phase_markers") if prior_state else _initial_phase_markers()
    if phase_markers is None:
        phase_markers = _initial_phase_markers()

    if not _due_for_poll(prior_state, now_utc):
        return []

    raw_flight = byair.get_flight(flight_id=flight_id)
    new_snapshot = _trim_to_snapshot(raw_flight)

    events: list[dict] = []

    # Scheduled departure lives at the top level of the flight-state
    # record, not inside the snapshot shape — resolve it before the
    # wake-rule call so first-cycle schedule-slip detection (#46) can
    # compare the fresh dep_time against it.
    scheduled_dep_time = (
        prior_state["scheduled_dep_time"] if prior_state else raw_flight.get("scheduledDepTime")
    )
    scheduled_arr_time = (
        prior_state["scheduled_arr_time"] if prior_state else raw_flight.get("scheduledArrTime")
    )

    # Delta-driven events from wake_rules, plus the gate-readout marker (#103).
    # The gate_assignment readout is evaluated here, before the other phase
    # markers, so its outcome can gate the gate_change filter below.
    boarding_lead_minutes = _resolve_boarding_lead_minutes(new_snapshot)
    readout_fired_before = phase_markers.get("gate_assignment_fired", False)
    readout_fired, readout_event = check_gate_assignment(
        scheduled_dep_time=scheduled_dep_time,
        boarding_lead_minutes=boarding_lead_minutes,
        snapshot=new_snapshot,
        phase_markers=phase_markers,
        now_utc=now_utc,
    )
    # The readout can never fire when the flight is already boarding/gone OR the
    # scheduled dep time is unparseable (no window to anchor on). In either case
    # gate_change must NOT be suppressed — degrade safely rather than mute a
    # corrupted-dep-time flight's gate moves forever.
    window_open = gate_assignment_window_open(
        scheduled_dep_time=scheduled_dep_time,
        boarding_lead_minutes=boarding_lead_minutes,
    )
    readout_unreachable = window_open is None or is_boarding_or_gone(new_snapshot)
    delta_events = detect_wake_events(prior_snapshot, new_snapshot, scheduled_dep_time)
    delta_events = _filter_gate_changes(
        delta_events,
        readout_fired_before=readout_fired_before,
        readout_fires_now=readout_fired,
        readout_unreachable=readout_unreachable,
    )
    events.extend(delta_events)
    if readout_fired and readout_event is not None:
        phase_markers["gate_assignment_fired"] = True
        events.append(readout_event)

    # Time-based events from phase_markers.

    fired, event = check_day_before(
        scheduled_dep_time=scheduled_dep_time,
        phase_markers=phase_markers,
        now_utc=now_utc,
    )
    if fired and event is not None:
        phase_markers["day_before_fired"] = True
        events.append(event)

    travel_time_seconds = _maybe_query_travel_time(
        maps=maps,
        origin=time_to_leave_origin,
        raw_flight=raw_flight,
        scheduled_dep_time=scheduled_dep_time,
        now_utc=now_utc,
        time_to_leave_already_fired=phase_markers.get("time_to_leave_fired", False),
    )
    fired, event = check_time_to_leave(
        scheduled_dep_time=scheduled_dep_time,
        travel_time_seconds=travel_time_seconds,
        phase_markers=phase_markers,
        now_utc=now_utc,
        snapshot=new_snapshot,
    )
    if fired and event is not None:
        phase_markers["time_to_leave_fired"] = True
        events.append(event)

    fired, event = check_arrival_logistics(
        scheduled_arr_time=scheduled_arr_time,
        phase_markers=phase_markers,
        now_utc=now_utc,
    )
    if fired and event is not None:
        phase_markers["arrival_logistics_fired"] = True
        events.append(event)

    # Persist updated state. write_flight_state requires every documented
    # required field; for a brand-new flight first seen on this cycle,
    # populate from the byAir payload.
    new_state = _build_flight_state(
        flight_id=flight_id,
        prior_state=prior_state,
        raw_flight=raw_flight,
        new_snapshot=new_snapshot,
        phase_markers=phase_markers,
        now_utc=now_utc,
    )
    write_flight_state(new_state)

    return events


def _due_for_poll(prior_state: dict | None, now_utc: datetime) -> bool:
    """Return True when a byAir poll is due.

    A flight with no prior state at all (never seeded) is always polled —
    there is no seeded departure time to range-check yet. For a seeded
    flight, a poll is due only when departure is inside the
    `_POLL_HORIZON_HOURS` window AND one of: no snapshot yet (sync_tripit
    seeded state but byAir has never been polled), or the cadence-ladder
    interval has elapsed since the last successful poll. A seeded flight
    departing beyond the horizon is never polled, even with no snapshot.
    """
    if prior_state is None:
        return True  # first cycle, no seeded departure time to range-check
    scheduled_dep = _parse_iso8601(prior_state.get("scheduled_dep_time"))
    if scheduled_dep is not None and scheduled_dep - now_utc > timedelta(hours=_POLL_HORIZON_HOURS):
        return False
    if prior_state.get("last_snapshot") is None:
        # sync_tripit seeds last_polled_at=now() with no snapshot; the
        # snapshot is the de-facto "byAir polled successfully" sentinel.
        return True
    last_polled = _parse_iso8601(prior_state.get("last_polled_at"))
    if last_polled is None:
        return True
    snapshot = prior_state.get("last_snapshot") or {}
    status = snapshot.get("computed_status", "scheduled")
    scheduled_arr = _parse_iso8601(prior_state.get("scheduled_arr_time"))
    interval_minutes = _interval_for(status, now_utc, scheduled_dep, scheduled_arr)
    return now_utc - last_polled >= timedelta(minutes=interval_minutes)


def _interval_for(
    status: str,
    now_utc: datetime,
    scheduled_dep: datetime | None,
    scheduled_arr: datetime | None,
) -> int:
    """Return the cadence interval (minutes) for a given (status, time)."""
    base = _BASE_CADENCE_MINUTES.get(status, 30)
    if status == "scheduled" and scheduled_dep is not None:
        time_to_dep = scheduled_dep - now_utc
        if time_to_dep <= timedelta(hours=6):
            return 10  # tightened to 10 min as departure approaches
        return 30
    if status in ("en_route", "departed") and scheduled_arr is not None:
        time_to_arr = scheduled_arr - now_utc
        if time_to_arr <= timedelta(minutes=30):
            return 2  # catch carousel reveal
    return base


def _resolve_time_to_leave_origin(
    *,
    home_address: str | None,
    now_utc: datetime,
) -> str | None:
    """Origin-resolution ladder for the time-to-leave Distance Matrix query.

    Delegates to `state.resolve_live_origin` — the single ladder shared with the
    airport-drive reconcile (fresh `current-location.json` → `home_address` →
    None), so the two never disagree on where the user is. Issue
    `jbaruch/nanoclaw-flight-assist#18`. The `home_address` rung is the
    trip-aware effective home resolved at cycle start (#122): the static
    residence off-trip, the current lodging on-trip.
    """
    return resolve_live_origin(home_address, now=now_utc)


def _maybe_query_travel_time(
    *,
    maps: MapsClient | None,
    origin: str | None,
    raw_flight: dict,
    scheduled_dep_time: str | None,
    now_utc: datetime,
    time_to_leave_already_fired: bool,
) -> int | None:
    """Query maps_client for travel time, only when time-to-leave is close.

    Returns None when:
    - MapsClient is None (key unset)
    - `origin` is None (origin-resolution yielded nothing — neither
      a fresh `current-location.json` snapshot nor a configured
      `home_address` was available at cycle-start)
    - Scheduled departure is more than _TIME_TO_LEAVE_QUERY_WINDOW_HOURS away
    - time_to_leave has already fired (no need to re-query)

    `origin` is resolved once per cycle in `_run_cycle` and passed
    through; this function does NOT re-read `current-location.json`,
    so every flight in the same cycle agrees on the user's location.
    """
    if maps is None or time_to_leave_already_fired or not origin:
        return None
    dep_dt = _parse_iso8601(scheduled_dep_time)
    if dep_dt is None:
        return None
    if dep_dt - now_utc > timedelta(hours=_TIME_TO_LEAVE_QUERY_WINDOW_HOURS):
        return None
    dep_airport = raw_flight.get("depAirport", {})
    destination = dep_airport.get("name") or dep_airport.get("code") or ""
    if not destination:
        return None
    try:
        result = maps.travel_time(origin=origin, destination=destination)
    except MapsError as maps_err:
        print(
            f"flight-assist precheck: maps error for flight: {maps_err}",
            file=sys.stderr,
        )
        return None
    except urllib.error.URLError as transport_err:
        # Transient transport failure to Google (network, DNS, API down).
        # Degrade just this maps query — return None so time_to_leave
        # defers to the next cycle. Per `coding-policy: error-handling`
        # "Specific Exceptions" + "Graceful Fallback".
        print(
            f"flight-assist precheck: maps transport error: {transport_err}",
            file=sys.stderr,
        )
        return None
    return result.in_traffic_seconds or result.duration_seconds


def _trim_to_snapshot(raw_flight: dict) -> dict:
    """Filter the ~13KB byAir flight payload to the ~1KB operational slice.

    Matches the last_snapshot shape in state-schema.md.
    """
    inbound_raw = raw_flight.get("inbound") or {}
    inbound = {
        "aircraft_model": inbound_raw.get("aircraft_model"),
        "registration": inbound_raw.get("registration"),
        "flew": inbound_raw.get("flew"),
        "predicted_delay_minutes": _extract_predicted_delay_minutes(inbound_raw),
    }
    position = raw_flight.get("position", {}).get("currentPosition", {})
    return {
        "code": raw_flight.get("code"),
        "computed_status": raw_flight.get("computed_status"),
        "computed_status_detail": raw_flight.get("computed_status_detail"),
        "computed_phase_progress": raw_flight.get("computed_phase_progress"),
        "computed_phase_risk": raw_flight.get("computed_phase_risk"),
        "computed_phase_overdue": raw_flight.get("computed_phase_overdue"),
        "dep_gate": raw_flight.get("depGate"),
        "arr_gate": raw_flight.get("arrGate"),
        "dep_terminal": raw_flight.get("depTerminal"),
        "arr_terminal": raw_flight.get("arrTerminal"),
        "dep_time": raw_flight.get("depTime"),
        "arr_time": raw_flight.get("arrTime"),
        "baggage": raw_flight.get("baggage"),
        "inbound": inbound,
        "position_lat": position.get("lat"),
        "position_lon": position.get("lon"),
    }


def _extract_predicted_delay_minutes(inbound: dict) -> int | None:
    predicted = inbound.get("predicted_delay")
    if not isinstance(predicted, dict):
        return None
    value = predicted.get("delay_minutes")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _build_flight_state(
    *,
    flight_id: int,
    prior_state: dict | None,
    raw_flight: dict,
    new_snapshot: dict,
    phase_markers: dict,
    now_utc: datetime,
) -> dict:
    """Construct a complete flight-state record per state-schema.md."""
    if prior_state is not None:
        scheduled_dep_time = prior_state["scheduled_dep_time"]
        scheduled_arr_time = prior_state["scheduled_arr_time"]
        ownership = prior_state["ownership"]
        trip_id = prior_state["trip_id"]
        dep_airport_id = prior_state["dep_airport_id"]
        arr_airport_id = prior_state["arr_airport_id"]
    else:
        scheduled_dep_time = raw_flight["scheduledDepTime"]
        scheduled_arr_time = raw_flight["scheduledArrTime"]
        ownership = raw_flight.get("ownership", "mine")
        trip_id = raw_flight.get("trip_id") or raw_flight.get("tripId") or 0
        dep_airport_id = raw_flight.get("depAirport", {}).get("id", 0)
        arr_airport_id = raw_flight.get("arrAirport", {}).get("id", 0)
    new_state = {
        "flight_id": flight_id,
        "code": raw_flight.get("code", ""),
        "ownership": ownership,
        "trip_id": trip_id,
        "scheduled_dep_time": scheduled_dep_time,
        "scheduled_arr_time": scheduled_arr_time,
        "dep_airport_id": dep_airport_id,
        "arr_airport_id": arr_airport_id,
        "last_polled_at": now_utc.isoformat().replace("+00:00", "Z"),
        "last_snapshot": new_snapshot,
        "phase_markers": phase_markers,
        "last_wake_at": prior_state["last_wake_at"] if prior_state else None,
        "last_wake_reason": prior_state["last_wake_reason"] if prior_state else None,
    }
    # Carry the calendar-reconcile ledger forward. The reconcile script
    # (calendar_reconcile.py) owns `calendar_events`, writing it on the wake
    # cycle; the precheck must not drop it when it rewrites state on each
    # poll, or the ledger — and the teardown tombstone it doubles as — would
    # be wiped every ~2 minutes. Preserve it verbatim when present.
    if prior_state is not None and "calendar_events" in prior_state:
        new_state["calendar_events"] = prior_state["calendar_events"]
    return new_state


def _filter_gate_changes(
    events: list[dict],
    *,
    readout_fired_before: bool,
    readout_fires_now: bool,
    readout_unreachable: bool,
) -> list[dict]:
    """Apply the #103 gate_change gating relative to the gate-readout anchor.

    The `gate_assignment` readout anchors gate info to the moment it becomes
    actionable; `gate_change` is gated against that anchor:

    - readout already fired on a prior cycle → all gate changes flow.
    - readout unreachable (flight already boarding / departed / gone, OR the
      scheduled dep time is unparseable so no window exists) → the readout will
      never fire, so all gate changes flow (never mute them forever — a flight
      first polled after departure, or one with a corrupted dep time, must still
      surface gate moves).
    - readout fires THIS cycle → drop only the now-redundant DEParture
      gate_change (the gate_assignment event already carries the dep gate), but
      keep an ARRival gate_change — the readout says nothing about the arr gate.
    - readout still pending (before the window, or in-window awaiting a dep
      gate) → suppress gate churn; it is recorded to state silently.

    Non-gate_change events pass through untouched.
    """
    if readout_fired_before or readout_unreachable:
        return events
    if readout_fires_now:
        return [
            e for e in events if not (e.get("reason") == "gate_change" and e.get("side") == "dep")
        ]
    return [e for e in events if e.get("reason") != "gate_change"]


def _resolve_boarding_lead_minutes(snapshot: dict) -> int:
    """Resolve the boarding-lead minutes for the gate-readout window (#103).

    Passes whatever lead inputs the snapshot carries to the same resolver
    `calendar_reconcile._resolve_lead` feeds the planner, so the readout window
    and the boarding calendar block agree on the lead. Today `_trim_to_snapshot`
    populates only `inbound.aircraft_model`, so the widebody lead resolves via
    the inbound-aircraft chain and the narrowbody default (30 min) covers the
    rest. The top-level `aircraft_model` and dep/arr airport coordinates are
    absent until the precheck stamps them (#55); the resolver reads them as
    None and they widen the window automatically once present.
    """
    inbound = snapshot.get("inbound") or {}
    return resolve_boarding_lead_minutes(
        aircraft_model=snapshot.get("aircraft_model"),
        inbound_aircraft_model=inbound.get("aircraft_model"),
        dep_lat=snapshot.get("dep_lat"),
        dep_lon=snapshot.get("dep_lon"),
        arr_lat=snapshot.get("arr_lat"),
        arr_lon=snapshot.get("arr_lon"),
    )


def _initial_phase_markers() -> dict:
    return {
        "day_before_fired": False,
        "time_to_leave_fired": False,
        "boarding_fired": False,
        "arrival_logistics_fired": False,
        "landed_acknowledged": False,
        "connection_at_risk_fired": False,
        "gate_assignment_fired": False,
    }


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


if __name__ == "__main__":
    sys.exit(main())
