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
from datetime import UTC, datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BUNDLE_DIR))

# trip_origin ships in the co-located travel-core bundle (same plugin). Resolve
# it at the runtime mount, falling back to the dev-clone sibling so the import
# works both on the NAS and in CI — same cross-bundle pattern as drive-engine.
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
from itinerary import CANCELLED, DIVERTED, Transport, parse_time  # noqa: E402
from maps_client import MapsClient, MapsError  # noqa: E402

# The co-shipped bundle path must be inserted before this import.
from notification_delivery import PHASE_BY_REASON, DeliveryStore, notification_store  # noqa: E402
from operator_tz import READER_TIMEOUT_SECONDS, OperatorTz, read_operator_tz  # noqa: E402
from phase_markers import (  # noqa: E402
    at_departure_airport,
    boarding_window_open,
    check_arrival_logistics,
    check_day_before,
    check_gate_assignment,
    check_time_to_leave,
    day_before_due,
    departure_airport_coords,
    gate_assignment_window_open,
    is_boarding_or_gone,
    positively_pre_departure,
)
from state import (  # noqa: E402
    StateError,
    fresh_live_coordinates,
    read_active_flights,
    read_config,
    read_flight_state,
    resolve_live_origin,
    write_flight_state,
)
from trip_origin import resolve_effective_home  # noqa: E402
from trip_window import evaluate_trip_window  # noqa: E402
from wake_rules import (  # noqa: E402
    DELAY_THRESHOLD_MINUTES,
    INBOUND_DELAY_THRESHOLD_MINUTES,
    detect_wake_events,
    is_real_boarding,
)

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
# precheck process at the budget this skill declares for itself —
# `precheck_timeout_ms: 30000` in SKILL.md — and surfaces the kill as
# `execfile-error`.
#
# That declaration and `_SCRIPT_KILL_BUDGET_SECONDS` below are ONE number in two
# places and MUST move together; the constant is the seconds form of the
# frontmatter value. Before jbaruch/nanoclaw#890 the 30s was a flat global the
# agent-runner applied to every precheck, so this constant only had to match a
# fleet-wide fact. #890 made the budget per-skill and removed the global: a
# skill that declares nothing now runs unbounded up to the container kill.
# flight-assist keeps an explicit 30s because it fires every 2 minutes — a
# wedged cycle running to the container timeout would still be holding the
# maintenance slot when the next fire is due.
#
# Polls run sequentially, so several
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
# to these crashes). Deriving the headroom from the call timeouts keeps it
# correct if any of them changes.
#
# The operator-zone reader (#300) runs inside `_process_flight` too, between the
# byAir poll and the Maps query, on the flight that first needs a `day_before`
# label. It spawns at most once per cycle, so its timeout is not in the base
# headroom: `_run_cycle` holds `_READER_RESERVE_SECONDS` back from a flight's
# start deadline only while the reader has not run yet AND that flight could
# fire its `day_before` this cycle (#312). A cycle whose flights have all fired
# theirs keeps the full budget.
#
# The departure-airport lookup (#349) is a third byAir-bounded call in the same
# `_process_flight`, between the poll and the Maps query. It runs only on a flight
# due to poll whose coordinates are not cached, with a fresh GPS fix, the
# `time_to_leave` marker unfired, Maps configured and departure inside
# `_TIME_TO_LEAVE_QUERY_WINDOW_HOURS`, so `_run_cycle` holds back
# `_AIRPORT_LOOKUP_RESERVE_SECONDS` from the start deadline of exactly those
# flights and leaves ordinary cycles their full budget. The reserve defers a
# flight, never the lookup: a lookup skipped near the kill would turn fresh
# at-airport GPS into permission to alert. When the reader and the lookup could
# both run, their reserves cannot both fit, so the flight keeps the lookup and
# labels its `day_before` with the explicit date, as with no zone.
_SCRIPT_KILL_BUDGET_SECONDS = 30.0
_INTERPRETER_TEARDOWN_HEADROOM_SECONDS = 4.0
_CYCLE_POLL_HEADROOM_SECONDS = (
    _BYAIR_CALL_TIMEOUT_SECONDS
    + _MAPS_CALL_TIMEOUT_SECONDS
    + _INTERPRETER_TEARDOWN_HEADROOM_SECONDS
)
_CYCLE_WALL_CLOCK_BUDGET_SECONDS = _SCRIPT_KILL_BUDGET_SECONDS - _CYCLE_POLL_HEADROOM_SECONDS
_READER_RESERVE_SECONDS = READER_TIMEOUT_SECONDS
_AIRPORT_LOOKUP_RESERVE_SECONDS = _BYAIR_CALL_TIMEOUT_SECONDS


def main() -> int:
    # outer-boundary-process-contract: this script is the scheduled-task's
    # outermost process boundary. The scheduler reads non-zero exit OR
    # invalid stdout as "skip waking the agent". A bare programming bug
    # bubbling out would silently disable the wake contract for every
    # subsequent run, so the outermost catch emits a safe-shape JSON +
    # writes the traceback to stderr per error-handling.md's carve-out.
    try:
        now_utc = datetime.now(UTC)
        # Defense-in-depth trip-window gate (#147). The host pre-spawn gate
        # (jbaruch/nanoclaw#754) should already keep the container from spawning
        # off-window; if one spawns anyway, bail here — before any byAir call —
        # reading the SAME travel-db.json with the SAME formula so the two layers
        # agree. Fail-open on a corrupt file never blinds an active trip.
        window = evaluate_trip_window(now_utc=now_utc)
        if not window.in_window:
            # Keep stdout to the documented contract; the human-readable
            # rationale goes to stderr for debug, not into the JSON payload.
            print(f"flight-assist precheck: {window.reason}", file=sys.stderr)
            _emit({"wake_agent": False, "data": {"reason": "outside_trip_window"}})
            return 0
        poll_failed: set[int] = set()
        events = _run_cycle(now_utc=now_utc, poll_failures=poll_failed)
        data: dict = {"events": events}
        if poll_failed:
            # A failed poll must not read as a quiet, healthy cycle (#326).
            # Alerting on a sustained outage is sync_tripit.py's job (its
            # consecutive-failure counter); this keeps the run log honest.
            data["error"] = "byair_poll_failed"
            data["poll_failed_ids"] = sorted(poll_failed)
        _emit({"wake_agent": bool(events), "data": data})
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
    operator_tz_reader: Callable[[], OperatorTz | None] = read_operator_tz,
    poll_failures: set[int] | None = None,
) -> list[dict]:
    failed = poll_failures if poll_failures is not None else set()
    active = list(dict.fromkeys(read_active_flights()))
    with notification_store(now_utc=now_utc) as delivery:
        delivery.prune(active_ids=set(active))
        verified: set[int] = set()
        events, excluded = _collect_cycle(
            now_utc=now_utc,
            monotonic=monotonic,
            operator_tz_reader=operator_tz_reader,
            poll_failures=failed,
            delivery=delivery,
            active_flight_ids=active,
            verified_ids=verified,
        )
        removed = {
            item["flight_id"] for item in events if item["event"]["reason"] == "removed_upstream"
        }
        phases = {
            (item["flight_id"], PHASE_BY_REASON[item["event"]["reason"]])
            for item in events
            if item["event"]["reason"] in PHASE_BY_REASON
        }
        delivery.reconcile(
            verified_ids=verified,
            current_phases=phases,
            is_current=lambda fid, event: _notice_is_current(fid, event, now_utc),
        )
        return delivery.due(
            active_ids=set(active), excluded_ids=excluded - removed, removed_ids=removed
        )


def _notice_is_current(flight_id: int | None, event: dict, now_utc: datetime) -> bool:
    if flight_id is None:
        return True
    if event["reason"] == "removed_upstream":
        # Reconciliation reaches this predicate only after a successful live
        # refresh. A current 404 is not verified and keeps its pending notice.
        return False
    state = read_flight_state(flight_id)
    if state is None:
        return False
    snapshot = state.get("last_snapshot") or {}
    status = snapshot.get("computed_status")
    reason = event["reason"]
    if status in {"cancelled", "diverted"}:
        return reason == status
    if reason in {"cancelled", "diverted"}:
        return False
    if reason == "gate_change":
        side = event.get("side")
        return side in {"dep", "arr"} and snapshot.get(f"{side}_gate") == event.get("to")
    if reason == "delay":
        current = _parse_iso8601(snapshot.get("dep_time"))
        observed = _parse_iso8601(event.get("new_dep_time"))
        return current is not None and current == observed
    if reason == "boarding_started":
        window = boarding_window_open(
            scheduled_dep_time=state.get("scheduled_dep_time"),
            boarding_lead_minutes=_resolve_boarding_lead_minutes(snapshot),
            snapshot=snapshot,
        )
        return is_real_boarding(snapshot, boarding_window_open=window, at=now_utc)
    if reason in {"inbound_delay_predicted", "inbound_delay_retracted"}:
        inbound = snapshot.get("inbound") or {}
        predicted = _positive_prediction(inbound)
        delayed = predicted is not None and predicted >= INBOUND_DELAY_THRESHOLD_MINUTES
        if reason == "inbound_delay_predicted":
            return (
                delayed
                and predicted == event.get("delay_minutes")
                and _predicted_departure(state.get("scheduled_dep_time"), snapshot)
                == event.get("predicted_time")
            )
        return not delayed and predicted == event.get("new_delay_minutes")
    if reason == "carousel_revealed":
        return snapshot.get("baggage") == event.get("baggage")
    return True


def _positive_prediction(inbound: dict) -> int | None:
    value = inbound.get("predicted_delay_minutes")
    return value if type(value) is int and value > 0 else None


def _predicted_departure(scheduled_dep_time: str | None, snapshot: dict) -> str | None:
    scheduled = _parse_iso8601(scheduled_dep_time)
    minutes = _positive_prediction(snapshot.get("inbound") or {})
    if scheduled is None or minutes is None:
        return None
    return (scheduled + timedelta(minutes=minutes)).isoformat()


def _refresh_pending_events(
    delivery: DeliveryStore, flight_id: int, snapshot: dict, events: list[dict]
) -> list[dict]:
    """Refresh unsent observations against their original, unannounced baseline."""
    result = list(events)

    def replace(reason: str, event: dict | None, *, side: str | None = None) -> None:
        result[:] = [
            item
            for item in result
            if item["reason"] != reason or side is not None and item.get("side") != side
        ]
        if event is not None:
            result.append(event)

    inbound = snapshot.get("inbound") or {}
    predicted = _positive_prediction(inbound)
    if predicted is not None and predicted >= INBOUND_DELAY_THRESHOLD_MINUTES:
        if delivery.pending_event(flight_id, "inbound_delay_predicted") is not None:
            replace(
                "inbound_delay_predicted",
                {
                    "reason": "inbound_delay_predicted",
                    "delay_minutes": predicted,
                    "predicted_time": inbound.get("predicted_time"),
                },
            )
    else:
        pending_prediction = delivery.pending_event(flight_id, "inbound_delay_predicted")
        last_delivered = delivery.last_confirmed_event(
            flight_id, {"inbound_delay_predicted", "inbound_delay_retracted"}
        )
        if pending_prediction is not None:
            if last_delivered is None or last_delivered["reason"] != "inbound_delay_predicted":
                replace("inbound_delay_retracted", None)
            else:
                replace(
                    "inbound_delay_retracted",
                    {
                        "reason": "inbound_delay_retracted",
                        "prev_delay_minutes": last_delivered["delay_minutes"],
                        "new_delay_minutes": predicted,
                    },
                )
        else:
            previous = delivery.pending_event(flight_id, "inbound_delay_retracted")
            if previous is not None:
                replace("inbound_delay_retracted", {**previous, "new_delay_minutes": predicted})

    previous = delivery.pending_event(flight_id, "delay")
    if previous is not None:
        observed = _parse_iso8601(previous.get("new_dep_time"))
        current = _parse_iso8601(snapshot.get("dep_time"))
        event = None
        if observed is not None and current is not None:
            baseline = observed - timedelta(minutes=previous["delay_minutes"])
            minutes = int((current - baseline).total_seconds() // 60)
            if abs(minutes) >= DELAY_THRESHOLD_MINUTES:
                event = {**previous, "delay_minutes": minutes, "new_dep_time": snapshot["dep_time"]}
        replace("delay", event)

    previous = delivery.pending_event(flight_id, "carousel_revealed")
    if previous is not None:
        baggage = snapshot.get("baggage")
        replace(
            "carousel_revealed", {**previous, "baggage": baggage} if baggage is not None else None
        )

    for side in ("dep", "arr"):
        previous = delivery.pending_event(flight_id, "gate_change", side=side)
        if previous is not None:
            gate = snapshot.get(f"{side}_gate")
            event = {**previous, "to": gate} if gate != previous["from"] else None
            replace("gate_change", event, side=side)
    return result


def _offer_notice(
    delivery: DeliveryStore,
    flight_id: int,
    event: dict,
    state: dict | None,
    now_utc: datetime,
    *,
    prior: dict | None = None,
) -> None:
    snapshot = (state or {}).get("last_snapshot") or {}
    reason = event["reason"]
    expiry = now_utc + timedelta(hours=2)
    if reason == "day_before":
        expiry = _parse_iso8601((state or {}).get("scheduled_dep_time")) or expiry
    elif reason in {"arrival_logistics", "carousel_revealed"}:
        arrival = _parse_iso8601(
            snapshot.get("arr_time") or (state or {}).get("scheduled_arr_time")
        )
        if arrival is not None:
            expiry = min(expiry, arrival + timedelta(hours=2))
    prior_poll = (prior if prior is not None else state or {}).get("last_polled_at")
    delivery.offer(flight_id, event, prior_poll=prior_poll, expires_at=expiry)


def _collect_cycle(
    *,
    now_utc: datetime,
    monotonic: Callable[[], float] = time.monotonic,
    operator_tz_reader: Callable[[], OperatorTz | None] = read_operator_tz,
    poll_failures: set[int],
    delivery: DeliveryStore,
    active_flight_ids: list[int],
    verified_ids: set[int],
) -> tuple[list[dict], set[int]]:
    """Execute one precheck cycle, return aggregated wake events.

    `poll_failures`, when given, receives the ids of flights whose byAir
    poll was attempted this cycle and failed (non-404 byAir error or
    transport error), so the caller can mark the run as failed (#326).

    `now_utc` is injected so tests can pin the clock without monkey-patching
    the `datetime` module (which breaks `fromisoformat()` deeper in the
    module). Production callers pass `datetime.now(timezone.utc)`.

    `monotonic` is injected so tests can drive the wall-clock budget
    deterministically without sleeping. Production callers use
    `time.monotonic`. It measures elapsed time for the poll-loop budget;
    `now_utc` is logical (cadence) time and must not be reused here because
    a test that pins `now_utc` would otherwise freeze the budget clock too.

    `operator_tz_reader` resolves the operator's current zone for the
    `day_before` day label (#300). The default spawns the core `current-tz`
    reader (`travel-core/operator_tz.py`), so it is wrapped below in a
    once-per-cycle memo and only ever called when a `day_before` is about
    to fire — a cycle whose flights have all fired theirs never pays for
    the subprocess, and every flight in one cycle sees the same zone. Its
    timeout is held back from a flight's start deadline only while that
    flight could still be the one to spawn it (#312).
    """
    config = read_config() or {}
    resolve_operator_tz = _memoized_operator_tz(operator_tz_reader)
    min_transfer_minutes = _resolve_min_transfer_minutes(config)

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
    # The time-to-leave origin, resolved once per cycle so every flight queries
    # Distance Matrix against the same position, and on first use, after that
    # flight's own poll (see `_CycleOrigin`).
    polled_ids = verified_ids
    forced_ids = delivery.retry_due_ids()
    cycle_origin = _CycleOrigin(
        home_address=config.get("home_address"),
        flight_ids=active_flight_ids,
        now_utc=now_utc,
        due=_due_ids(active_flight_ids, now_utc) | forced_ids,
        polled=polled_ids,
        removed=removed_upstream_ids,
    )
    # Which flights could spawn the zone reader this cycle: their deadline
    # carries the reader reserve until the reader has run (#312).
    may_spawn_reader = {
        fid for fid in active_flight_ids if _may_spawn_reader(fid, now_utc, force=fid in forced_ids)
    }
    # One fix per cycle: the pre-pass below and every flight's presence check
    # read the same answer, so a fix landing mid-cycle cannot add a lookup the
    # deadline did not reserve for.
    cycle_gps = fresh_live_coordinates(now_utc)
    may_lookup_airport = {
        fid
        for fid in active_flight_ids
        if _may_lookup_airport(
            fid,
            now_utc,
            force=fid in forced_ids,
            gps=cycle_gps,
            maps_configured=maps is not None,
            delivery=delivery,
        )
    }
    poll_deadline = monotonic() + _CYCLE_WALL_CLOCK_BUDGET_SECONDS
    for index, flight_id in enumerate(active_flight_ids):
        lookup_reserve = _AIRPORT_LOOKUP_RESERVE_SECONDS if flight_id in may_lookup_airport else 0.0
        reader_reserve = (
            _READER_RESERVE_SECONDS
            if flight_id in may_spawn_reader and not resolve_operator_tz.resolved
            else 0.0
        )
        flight_operator_tz = resolve_operator_tz
        if lookup_reserve + reader_reserve >= _CYCLE_WALL_CLOCK_BUDGET_SECONDS:
            # Both calls cannot be held back from one start deadline. Keep the
            # lookup, which presence safety needs, and skip the reader: with no
            # zone the `day_before` label is the explicit date.
            reader_reserve = 0.0
            flight_operator_tz = _no_operator_tz
        reserve = lookup_reserve + reader_reserve
        if monotonic() >= poll_deadline - reserve:
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
                resolve_operator_tz=flight_operator_tz,
                resolve_gps=lambda: cycle_gps,
                delivery=delivery,
                force=flight_id in forced_ids,
                verified_ids=verified_ids,
            )
        except ByAirError as byair_err:
            # 404 on the byAir side means the flight is no longer
            # tracked upstream; surface as a removed_upstream event.
            if byair_err.error_type == "not_found":
                removed = {"reason": "removed_upstream"}
                prior = read_flight_state(flight_id)
                _offer_notice(delivery, flight_id, removed, prior, now_utc)
                aggregated_events.append({"flight_id": flight_id, "event": removed})
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
    if poll_failures is not None:
        poll_failures.update(poll_failed_ids)
    excluded_ids = removed_upstream_ids | poll_failed_ids | deferred_ids
    risk_candidate_ids = [fid for fid in active_flight_ids if fid not in excluded_ids]
    aggregated_events.extend(
        _check_connection_risks(
            active_flight_ids=risk_candidate_ids,
            now_utc=now_utc,
            min_transfer_minutes=min_transfer_minutes,
            delivery=delivery,
        )
    )
    return aggregated_events, excluded_ids


class _OperatorTzMemo:
    """The zone reader, spawned at most once per cycle and only on demand.

    Calling it runs `reader` the first time and caches the answer (the IANA
    name, or None when no zone is available); later calls return the cache.
    Nothing runs until something asks, so the subprocess is paid for only by
    a cycle that is about to fire a `day_before` (#300). `resolved` tells the
    poll loop whether the spawn is still ahead, so the loop reserves its
    timeout only until then (#312).
    """

    def __init__(self, reader: Callable[[], OperatorTz | None]) -> None:
        self._reader = reader
        self._cache: list[str | None] = []

    @property
    def resolved(self) -> bool:
        return bool(self._cache)

    def __call__(self) -> str | None:
        if not self._cache:
            zone = self._reader()
            self._cache.append(zone.tz if zone is not None else None)
        return self._cache[0]


def _due_ids(flight_ids: list[int], now_utc: datetime) -> set[int]:
    """The flights due a poll this cycle. A record that cannot be read counts as
    due, so its snapshot is never trusted; it fails at its own turn in the poll
    loop, never here before the flights ahead of it poll."""
    due = set()
    for flight_id in flight_ids:
        try:
            state = read_flight_state(flight_id)
        except (StateError, OSError):
            due.add(flight_id)
            continue
        if _due_for_poll(state, now_utc):
            due.add(flight_id)
    return due


class UnplaceableFlight(ValueError):
    """A tracked flight with a route whose departure and arrival do not read
    as a span: he is moving, or has moved, at times nobody can place."""


class _CycleOrigin:
    """One cycle's time-to-leave origin, resolved on first use and then fixed.

    Trip-aware (#122, #335): "home" is the itinerary's position: the static
    residence at home, the current lodging or trip city away. It is read with
    byAir's flights as well as TripIt's, so a trip only byAir tracks still moves
    the operator. None away with nothing resolvable disables routing via the
    existing no-home handling. It is resolved when the first time-to-leave
    query is due, after that flight's own poll, whose fresh snapshot the
    caller hands in. A recalled snapshot is a hint, never live state: every
    flight `due` this cycle and not yet `polled` by then (failed, deferred, or
    still ahead in the loop, where a 404 may yet come for it) is left out, and
    TripIt's copy of it, if any, books it. A flight not due keeps the snapshot
    its cadence vouches for. A flight byAir no longer tracks (`removed`, a 404
    this cycle) is left out too. A flight whose movement does not read
    (`UnplaceableFlight`) leaves no origin: routing waits for the next cycle."""

    def __init__(
        self,
        *,
        home_address: str | None,
        flight_ids: list[int],
        now_utc: datetime,
        due: set[int],
        polled: set[int],
        removed: set[int] | frozenset[int] = frozenset(),
        read_state: Callable[[int], dict | None] = read_flight_state,
    ):
        self._home, self._ids, self._now = home_address, flight_ids, now_utc
        self._due, self._polled, self._read = due, polled, read_state
        self._removed = removed
        self._resolved = False
        self._origin: str | None = None

    def __call__(self, fresh: dict[int, dict] | None = None) -> str | None:
        if not self._resolved:
            fresh = fresh or {}
            unverified = self._due - self._polled - set(fresh)
            try:
                transports = byair_transports(
                    [i for i in self._ids if i not in self._removed | unverified],
                    read_state=self._read,
                    fresh=fresh,
                )
            except UnplaceableFlight as exc:
                print(f"[flight-assist] {exc}", file=sys.stderr)
                self._resolved = True
                return None
            home = resolve_effective_home(self._home, now=self._now, transports=transports)
            self._origin = _resolve_time_to_leave_origin(home_address=home, now_utc=self._now)
            self._resolved = True
        return self._origin


def byair_transports(
    flight_ids: list[int],
    *,
    read_state: Callable[[int], dict | None] = read_flight_state,
    fresh: dict[int, dict] | None = None,
) -> list[Transport]:
    """byAir's tracked flights as itinerary transports (#335), from this skill's
    own byAir poll: the snapshot in `fresh` (polled this cycle) or the stored
    `last_snapshot`, its airport codes, and its live times over the scheduled
    ones. The caller passes only flights whose snapshot it vouches for.

    A cancelled flight moves nobody and takes TripIt's copy of it with it. A
    diverted one keeps its times with no destination airport: he landed
    somewhere, not where it was going, so the itinerary judges that landing by
    the trip rather than by an airport. Each carries its booked departure, so a
    long delay still matches TripIt's copy. A flight never polled yet has no
    snapshot and is left out: the poll reaches every tracked flight before it
    departs, so that flight is
    still ahead, and the position now depends only on flights already flown.
    A record that cannot be read is left out too; the poll loop reports it at
    its own turn. A routed flight whose live and booked times give no span
    raises `UnplaceableFlight`: leaving it out would hand its movement to
    TripIt's booked copy."""
    transports: list[Transport] = []
    for flight_id in flight_ids:
        try:
            record = read_state(flight_id)
        except (StateError, OSError) as exc:
            # The poll loop meets the same record at its own turn and reports it
            # there; this pass only leaves the flight off the itinerary.
            print(f"[flight-assist] flight {flight_id} state unreadable ({exc})", file=sys.stderr)
            continue
        if record is None:
            continue
        snapshot = (fresh or {}).get(flight_id) or record.get("last_snapshot")
        if not isinstance(snapshot, dict):
            continue
        origin, destination = snapshot.get("dep_airport_code"), snapshot.get("arr_airport_code")
        if not isinstance(origin, str) or not isinstance(destination, str):
            continue
        status = snapshot.get("computed_status")
        booked = parse_time(record.get("scheduled_dep_time"))
        departs = parse_time(snapshot.get("dep_time")) or booked
        arrives = parse_time(snapshot.get("arr_time")) or parse_time(
            record.get("scheduled_arr_time")
        )
        if status == CANCELLED and departs is not None:
            # A cancellation needs only its route and booked departure to take
            # TripIt's copy with it; it moves nobody.
            arrives = arrives if arrives is not None and arrives > departs else departs
        elif departs is None or arrives is None or arrives <= departs:
            raise UnplaceableFlight(
                f"flight {flight_id} {origin}-{destination}: departure {departs} and "
                f"arrival {arrives} give no span; time-to-leave routing waits for a "
                f"poll with a live arrival, or fix flight-{flight_id}.json"
            )
        transports.append(
            Transport(
                departs,
                arrives,
                origin,
                None if status == DIVERTED else destination,
                scheduled=booked,
                status=status if status in (CANCELLED, DIVERTED) else None,
            )
        )
    return transports


def _memoized_operator_tz(reader: Callable[[], OperatorTz | None]) -> _OperatorTzMemo:
    """Wrap the zone reader in a once-per-cycle, on-demand memo."""
    return _OperatorTzMemo(reader)


def _may_spawn_reader(flight_id: int, now_utc: datetime, *, force: bool = False) -> bool:
    """Whether processing this flight this cycle could spawn the zone reader.

    `_process_flight` asks for the zone only when `day_before_due` holds for
    the flight's persisted departure and markers, so the same predicate
    answers here, behind the same `_due_for_poll` gate: a flight inside its
    cadence interval returns before any label is asked for. A flight with no
    state yet is first seen this cycle: its departure is unknown until byAir
    answers, so it may. So may a flight whose record does not read (corrupt,
    or an I/O error): this pre-pass only sizes a deadline, and the record
    still fails loudly when its own turn to poll comes, after the flights
    ahead of it have polled.
    """
    try:
        prior_state = read_flight_state(flight_id)
    except (StateError, OSError):
        return True
    if prior_state is None:
        return True
    if not force and not _due_for_poll(prior_state, now_utc):
        return False
    return day_before_due(
        scheduled_dep_time=prior_state.get("scheduled_dep_time"),
        phase_markers=prior_state.get("phase_markers") or {},
        now_utc=now_utc,
    )


def _no_operator_tz() -> None:
    """The zone resolver of a flight that must not spawn the reader this cycle."""
    return None


def _may_need_airport_lookup(
    *,
    gps_fresh: bool,
    maps_configured: bool,
    time_to_leave_fired: bool,
    scheduled_dep_time: str | None,
    now_utc: datetime,
) -> bool:
    """Whether a `time_to_leave` could fire, so airport coordinates are worth a call.

    Mirrors the gates `_maybe_query_travel_time` and `check_time_to_leave` apply
    before presence can matter: a fresh fix to compare, Maps configured, the
    marker unfired, and departure inside the travel-query window. Outside them
    the gate stays silent whatever the presence verdict is, so skipping the
    lookup there changes no alert (#349).
    """
    if not gps_fresh or not maps_configured or time_to_leave_fired:
        return False
    dep_dt = _parse_iso8601(scheduled_dep_time)
    if dep_dt is None:
        return False
    return dep_dt - now_utc <= timedelta(hours=_TIME_TO_LEAVE_QUERY_WINDOW_HOURS)


def _may_lookup_airport(
    flight_id: int,
    now_utc: datetime,
    *,
    force: bool,
    gps: tuple[float, float] | None,
    maps_configured: bool,
    delivery: DeliveryStore,
) -> bool:
    """Whether processing this flight this cycle could call `get_airport`.

    Same inputs as `_process_flight`, behind the same `_due_for_poll` gate, so
    the pre-pass reserve and the in-flight lookup agree. A flight with no state
    is first seen this cycle: its departure is unknown until byAir answers, so
    it may. So may a flight whose record does not read; the record still fails
    loudly at its own turn to poll (#312 `_may_spawn_reader`).
    """
    if gps is None or not maps_configured:
        return False
    try:
        prior_state = read_flight_state(flight_id)
    except (StateError, OSError):
        return True
    if prior_state is None:
        return True
    if not force and not _due_for_poll(prior_state, now_utc):
        return False
    if _cached_departure_coords(prior_state.get("last_snapshot")) is not None:
        return False
    fired = bool((prior_state.get("phase_markers") or {}).get("time_to_leave_fired")) or (
        "time_to_leave_fired" in delivery.confirmed_markers(flight_id)
    )
    return _may_need_airport_lookup(
        gps_fresh=True,
        maps_configured=True,
        time_to_leave_fired=fired,
        scheduled_dep_time=prior_state.get("scheduled_dep_time"),
        now_utc=now_utc,
    )


def _check_connection_risks(
    *,
    active_flight_ids: list[int],
    now_utc: datetime,
    min_transfer_minutes: int,
    delivery: DeliveryStore,
) -> list[dict]:
    """Queue cross-flight risks without treating a wake as delivery."""
    flight_states: list[dict] = []
    for fid in active_flight_ids:
        state = read_flight_state(fid)
        if state is not None:
            state = {**state, "phase_markers": dict(state["phase_markers"])}
            for marker in delivery.confirmed_markers(fid):
                state["phase_markers"][marker] = True
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
        _offer_notice(delivery, leg2_flight_id, event, leg2_state, now_utc)
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
    time_to_leave_origin: Callable[[dict[int, dict]], str | None],
    delivery: DeliveryStore,
    verified_ids: set[int],
    force: bool = False,
    resolve_operator_tz: Callable[[], str | None] = lambda: None,
    resolve_gps: Callable[[], tuple[float, float] | None] | None = None,
) -> list[dict]:
    """Process a single flight: cadence-gate, fetch, diff, emit events.

    `time_to_leave_origin` is `_run_cycle`'s once-per-cycle origin
    (`_CycleOrigin`), handed this flight's fresh snapshot and consulted only
    when the time-to-leave query runs, so every flight processed in the same
    cycle agrees on the user's location even when the host-orchestrator-owned
    `current-location.json` is rewritten mid-cycle.

    `resolve_operator_tz` is `_run_cycle`'s once-per-cycle memo of the
    operator's zone; it is consulted only when `phase_markers.day_before_due`
    says the `day_before` fires this cycle, so the reader subprocess never
    runs for a flight that is not about to be labelled. The default (no
    zone) is the standalone-call shape and yields the explicit-date label.

    `resolve_gps` is `_run_cycle`'s once-per-cycle fresh fix, the one its
    airport-lookup reserve was sized from; the default reads it here.
    """
    prior_state = read_flight_state(flight_id)
    prior_snapshot = prior_state.get("last_snapshot") if prior_state else None
    phase_markers = prior_state.get("phase_markers") if prior_state else _initial_phase_markers()
    if phase_markers is None:
        phase_markers = _initial_phase_markers()

    phase_markers = dict(phase_markers)
    for marker in delivery.confirmed_markers(flight_id):
        phase_markers[marker] = True
    if not force and not _due_for_poll(prior_state, now_utc):
        return []

    raw_flight = byair.get_flight(flight_id=flight_id)
    new_snapshot = _trim_to_snapshot(raw_flight)
    verified_ids.add(flight_id)
    gps = resolve_gps() if resolve_gps is not None else fresh_live_coordinates(now_utc)
    scheduled_dep_time = (
        prior_state["scheduled_dep_time"] if prior_state else raw_flight.get("scheduledDepTime")
    )
    dep_lat, dep_lon = _departure_coords_for_presence(
        prior_snapshot=prior_snapshot,
        raw_flight=raw_flight,
        prior_state=prior_state,
        byair=byair,
        gps=gps,
        lookup_wanted=_may_need_airport_lookup(
            gps_fresh=gps is not None,
            maps_configured=maps is not None,
            time_to_leave_fired=bool(phase_markers.get("time_to_leave_fired", False)),
            scheduled_dep_time=scheduled_dep_time,
            now_utc=now_utc,
        ),
    )
    if dep_lat is not None and dep_lon is not None:
        new_snapshot["dep_lat"] = dep_lat
        new_snapshot["dep_lon"] = dep_lon

    events: list[dict] = []

    # Scheduled departure lives at the top level of the flight-state
    # record, not inside the snapshot shape — resolved above, before the
    # wake-rule call, so first-cycle schedule-slip detection (#46) can
    # compare the fresh dep_time against it.
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
    # The planned boarding window (effective dep − lead: byAir's live dep_time
    # when the snapshot has one, else scheduled — the same instant and lead the
    # boarding block uses): byAir's "boarding" label before it is premature by construction
    # (#295). Each snapshot is judged at the instant it was polled — the fresh
    # one now, the prior one at its own `last_polled_at` — so the first
    # in-window poll fires the transition even when the raw label never moved.
    boarding_open = boarding_window_open(
        scheduled_dep_time=scheduled_dep_time,
        boarding_lead_minutes=boarding_lead_minutes,
        snapshot=new_snapshot,
    )
    readout_unreachable = window_open is None or is_boarding_or_gone(
        new_snapshot, boarding_window_open=boarding_open, at=now_utc
    )
    # The prior snapshot is judged against the window as it stood at its own
    # poll: its own effective departure and its own lead (Copilot on #306).
    prev_boarding_open = (
        boarding_window_open(
            scheduled_dep_time=scheduled_dep_time,
            boarding_lead_minutes=_resolve_boarding_lead_minutes(prior_snapshot),
            snapshot=prior_snapshot,
        )
        if prior_snapshot
        else None
    )
    # The notification projection derives its clock without changing the persisted schema-7 slice.
    notification_snapshot = {
        **new_snapshot,
        "inbound": {
            **new_snapshot["inbound"],
            "predicted_time": _predicted_departure(scheduled_dep_time, new_snapshot),
        },
    }
    delta_events = detect_wake_events(
        prior_snapshot,
        notification_snapshot,
        scheduled_dep_time,
        boarding_window_open=boarding_open,
        now_utc=now_utc,
        prev_polled_at=_parse_iso8601(prior_state.get("last_polled_at")) if prior_state else None,
        prev_boarding_window_open=prev_boarding_open,
    )
    delta_events = _refresh_pending_events(delivery, flight_id, notification_snapshot, delta_events)
    delta_events = _filter_gate_changes(
        delta_events,
        readout_fired_before=readout_fired_before,
        readout_fires_now=readout_fired,
        readout_unreachable=readout_unreachable,
    )
    delta_events = [
        event
        for event in delta_events
        if not phase_markers.get(PHASE_BY_REASON.get(event["reason"], ""), False)
    ]
    events.extend(delta_events)
    if readout_fired and readout_event is not None:
        events.append(readout_event)

    # Time-based events from phase_markers.

    # The zone is asked for only when the day_before gate is open this cycle:
    # a flight already past its marker, more than a day out, or with an
    # unparseable departure never triggers the reader spawn, and the memo
    # makes a second ask in the same cycle free (#300).
    operator_tz = (
        resolve_operator_tz()
        if day_before_due(
            scheduled_dep_time=scheduled_dep_time, phase_markers=phase_markers, now_utc=now_utc
        )
        else None
    )
    fired, event = check_day_before(
        scheduled_dep_time=scheduled_dep_time,
        phase_markers=phase_markers,
        now_utc=now_utc,
        operator_tz=operator_tz,
    )
    if fired and event is not None:
        events.append(event)

    travel_time_seconds = _maybe_query_travel_time(
        maps=maps,
        resolve_origin=lambda: time_to_leave_origin({flight_id: new_snapshot}),
        raw_flight=raw_flight,
        scheduled_dep_time=scheduled_dep_time,
        now_utc=now_utc,
        time_to_leave_already_fired=phase_markers.get("time_to_leave_fired", False),
    )
    at_airport = at_departure_airport(
        origin_lat=None if gps is None else gps[0],
        origin_lon=None if gps is None else gps[1],
        dep_lat=dep_lat,
        dep_lon=dep_lon,
    )
    prev_polled_at, prior_interval_minutes = _prior_poll_cadence(
        prior_state,
        now_utc,
        _parse_iso8601(scheduled_dep_time) if isinstance(scheduled_dep_time, str) else None,
        _parse_iso8601(scheduled_arr_time) if isinstance(scheduled_arr_time, str) else None,
    )
    fired, event = check_time_to_leave(
        scheduled_dep_time=scheduled_dep_time,
        travel_time_seconds=travel_time_seconds,
        phase_markers=phase_markers,
        now_utc=now_utc,
        snapshot=new_snapshot,
        boarding_lead_minutes=boarding_lead_minutes,
        at_airport=at_airport,
        prev_polled_at=prev_polled_at,
        prior_poll_interval_minutes=prior_interval_minutes,
    )
    if fired and event is not None:
        events.append(event)

    fired, event = check_arrival_logistics(
        scheduled_arr_time=scheduled_arr_time,
        phase_markers=phase_markers,
        now_utc=now_utc,
    )
    if fired and event is not None:
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
    for event in events:
        _offer_notice(delivery, flight_id, event, new_state, now_utc, prior=prior_state or {})
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


def _prior_poll_cadence(
    prior_state: dict | None,
    now_utc: datetime,
    scheduled_dep: datetime | None,
    scheduled_arr: datetime | None,
) -> tuple[datetime | None, int]:
    """The last successful poll and the cadence that spaced the next one (#349).

    `check_time_to_leave` treats the first actual poll at or after `leave_by`
    as the timely notice. Which poll that is comes from the PRIOR state, the
    same snapshot `_due_for_poll` read to schedule this one: the status the new
    snapshot reports can tighten or loosen the cadence on the very poll that
    crosses `leave_by`, and must not move the window.

    `(None, 0)` when nothing has polled successfully yet: a state seeded by
    sync_tripit carries `last_polled_at` but no snapshot, so the snapshot is the
    sentinel, as in `_due_for_poll`. The interval is 0 when the prior status is
    not a documented pre-departure label, so an unrecognised or empty status
    never widens the window.
    """
    if prior_state is None:
        return None, 0
    snapshot = prior_state.get("last_snapshot")
    if not isinstance(snapshot, dict):
        return None, 0
    polled_at = _parse_iso8601(prior_state.get("last_polled_at"))
    if polled_at is None:
        return None, 0
    if not positively_pre_departure(snapshot):
        return polled_at, 0
    return polled_at, _interval_for(
        snapshot["computed_status"], now_utc, scheduled_dep, scheduled_arr
    )


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

    Delegates to `state.resolve_live_origin` — the origin ladder (fresh
    `current-location.json` → `home_address` → None). Issue
    `jbaruch/nanoclaw-flight-assist#18`. The `home_address` rung is the
    trip-aware effective home resolved at cycle start (#122): the static
    residence off-trip, the current lodging on-trip.
    """
    return resolve_live_origin(home_address, now=now_utc)


def _cached_departure_coords(snapshot: object) -> tuple[float, float] | None:
    """Valid `dep_lat`/`dep_lon` persisted on a snapshot, else None."""
    if not isinstance(snapshot, dict):
        return None
    lat, lon = departure_airport_coords(
        {"lat": snapshot.get("dep_lat"), "lon": snapshot.get("dep_lon")}
    )
    if lat is None or lon is None:
        return None
    return lat, lon


def _departure_coords_for_presence(
    *,
    prior_snapshot: dict | None,
    raw_flight: dict,
    prior_state: dict | None,
    byair: ByAirClient,
    gps: tuple[float, float] | None,
    lookup_wanted: bool,
) -> tuple[float | None, float | None]:
    """Departure-airport `lat`/`lon` for GPS presence (#349).

    Reuses cached `last_snapshot.dep_lat`/`dep_lon` when both are valid.
    Otherwise, only when a fresh GPS fix exists this cycle and
    `lookup_wanted` (`_may_need_airport_lookup`: a `time_to_leave` could
    still fire), pays one `byair.get_airport(depAirport.id)` using the
    documented get_airport coordinate keys. Nested `get_flight.depAirport`
    is not assumed to carry `lat`/`lon`. Missing or failed lookup skips
    presence this cycle; a later GPS-present poll retries until coords
    persist.
    """
    cached = _cached_departure_coords(prior_snapshot)
    if cached is not None:
        return cached
    if gps is None or not lookup_wanted:
        return None, None
    airport_id: object = None
    if prior_state is not None:
        airport_id = prior_state.get("dep_airport_id")
    if not isinstance(airport_id, int) or isinstance(airport_id, bool):
        dep_airport = raw_flight.get("depAirport")
        airport_id = dep_airport.get("id") if isinstance(dep_airport, dict) else None
    if not isinstance(airport_id, int) or isinstance(airport_id, bool):
        return None, None
    try:
        payload = byair.get_airport(airport_id)
    except ByAirError as byair_err:
        print(
            f"flight-assist precheck: byAir get_airport({airport_id}) failed: {byair_err}",
            file=sys.stderr,
        )
        return None, None
    except urllib.error.URLError as transport_err:
        print(
            f"flight-assist precheck: byAir get_airport({airport_id}) transport error: "
            f"{transport_err}",
            file=sys.stderr,
        )
        return None, None
    return departure_airport_coords(payload)


def _maybe_query_travel_time(
    *,
    maps: MapsClient | None,
    resolve_origin: Callable[[], str | None],
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

    `resolve_origin` is the cycle's origin (`_CycleOrigin`), called only
    once the query is otherwise due; this function does NOT re-read
    `current-location.json`, so every flight in the same cycle agrees on the
    user's location.
    """
    if maps is None or time_to_leave_already_fired:
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
    origin = resolve_origin()
    if not origin:
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
    dep_airport = raw_flight.get("depAirport") or {}
    arr_airport = raw_flight.get("arrAirport") or {}
    return {
        "code": raw_flight.get("code"),
        # Resolved airport identity captured off the same byAir payload the poll
        # already fetched (the airport dict carries `code`/`name` — see the
        # time-to-leave path's `depAirport.get("name")`). Persisted so the
        # day-before / arrival compose renders the airport deterministically
        # instead of free-typing a name off the numeric id alone (#159 Bug 2).
        "dep_airport_code": dep_airport.get("code"),
        "dep_airport_name": dep_airport.get("name"),
        "arr_airport_code": arr_airport.get("code"),
        "arr_airport_name": arr_airport.get("name"),
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
        # #159 Bug 1: `code` is the MARKETING designator (e.g. DL4908), a
        # seed-time identity field sync_tripit captures from byAir list_trips.
        # The poll uses get_flight, whose top-level `code` is the OPERATING
        # designator (9E4908) — get_flight carries no structured marketing code
        # (only the free-text `note`). So `code` is preserved from the seed
        # alongside the other identity fields above, never overwritten by the
        # poll. Absent a prior seed, the poll's code is the best available.
        code = prior_state["code"]
    else:
        scheduled_dep_time = raw_flight["scheduledDepTime"]
        scheduled_arr_time = raw_flight["scheduledArrTime"]
        ownership = raw_flight.get("ownership", "mine")
        trip_id = raw_flight.get("trip_id") or raw_flight.get("tripId") or 0
        dep_airport_id = raw_flight.get("depAirport", {}).get("id", 0)
        arr_airport_id = raw_flight.get("arrAirport", {}).get("id", 0)
        code = raw_flight.get("code", "")
    # Keep the persisted snapshot's `code` aligned with the canonical display
    # code so no downstream reader can ever surface the operating designator.
    new_snapshot = {**new_snapshot, "code": code}
    new_state = {
        "flight_id": flight_id,
        "code": code,
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
    and the boarding calendar block agree on the lead. `_trim_to_snapshot`
    populates `inbound.aircraft_model`; `dep_lat`/`dep_lon` arrive when the
    precheck caches `get_airport` coordinates for GPS presence (#349).
    `arr_lat`/`arr_lon` and top-level `aircraft_model` stay absent until
    those byAir fields are confirmed (#55). The resolver reads missing
    inputs as None and widens the window automatically once present.
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
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


if __name__ == "__main__":
    sys.exit(main())
