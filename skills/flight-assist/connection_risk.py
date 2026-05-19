"""Connection-risk derivation for the flight-assist precheck.

Where `wake_rules.py` detects events from single-flight snapshot deltas
and `phase_markers.py` detects them from wall-clock time, this module
detects them from CROSS-flight context — leg-1's live arrival vs
leg-2's scheduled departure on the same trip.

The capability (README #4): for multi-leg itineraries with tight
transfer windows, alert when leg-1 is running late so a rebook
decision is possible BEFORE leg-1 takes off (when the user still has
agency on the ground).

Fires once per leg-2 flight (the `connection_at_risk_fired` phase
marker on leg-2's state). After firing, the alert won't re-fire even
if the delay magnitude grows further — the user already knows the
connection is tight; what they need is enough lead time to act.

Inputs are loaded by the precheck from existing on-disk per-flight
state — no fresh byAir call is needed here, because the cadence ladder
already keeps each leg's snapshot at the appropriate freshness. The
per-flight `trip_id` stored at sync time is the grouping key.

Pure functions: no I/O, no state mutation. The caller (precheck.py)
sets the marker flag in state after firing so subsequent cycles
don't re-emit.

stdlib-only: `datetime` per `coding-policy: dependency-management`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_MIN_TRANSFER_MINUTES = 45

# Don't fire farther out than this from leg-1's scheduled departure —
# delay projections that far in advance are typically speculative, and
# a rebook decision needs day-of (or eve-of) certainty to be useful.
_LEG1_LOOKAHEAD_HOURS = 24

# Status values at which leg-1 connection-risk is no longer actionable.
# `landed`: the user already did or did not make the connection (the
# answer is observable). `cancelled` / `diverted`: a more specific
# alert path fires via wake_rules.
_LEG1_TERMINAL_STATUSES = frozenset({"landed", "cancelled", "diverted"})


def detect_connection_risks(
    *,
    flight_states: list[dict],
    now_utc: datetime,
    min_transfer_minutes: int = DEFAULT_MIN_TRANSFER_MINUTES,
) -> list[tuple[int, dict]]:
    """Return per-leg-2 connection-risk events for tight transfers.

    Returns a list of `(leg2_flight_id, event_dict)` tuples. The caller
    (precheck.py) flips `phase_markers["connection_at_risk_fired"]` on
    each leg-2 state record after the event is emitted, so subsequent
    cycles don't re-fire.

    `flight_states` is the full on-disk list of per-flight records
    (each matches the shape in state-schema.md). The function groups by
    `trip_id`, sorts each group by `scheduled_dep_time`, and walks
    consecutive pairs where leg-1's arrival airport equals leg-2's
    departure airport.

    A pair fires `connection_at_risk` when ALL of:

    - Leg-1's scheduled departure is within `_LEG1_LOOKAHEAD_HOURS` of `now_utc`
    - Leg-1 status is not in {landed, cancelled, diverted}
    - Leg-2's `connection_at_risk_fired` marker is False
    - The projected transfer window is less than `min_transfer_minutes`

    The projected transfer window is computed as:

        scheduled_dep(leg-2) - projected_arr(leg-1)

    where `projected_arr(leg-1)` comes from leg-1's live `arr_time`
    snapshot when present, otherwise from `scheduled_arr_time` (no live
    update yet means scheduled is the best estimate).
    """
    events: list[tuple[int, dict]] = []
    for trip_group in _group_by_trip(flight_states):
        events.extend(
            _detect_for_trip(
                trip_group=trip_group,
                now_utc=now_utc,
                min_transfer_minutes=min_transfer_minutes,
            )
        )
    return events


def _group_by_trip(flight_states: list[dict]) -> list[list[dict]]:
    """Group flight states by `trip_id`, sorted by scheduled_dep_time inside each group.

    Single-leg trips (one flight per trip_id) are excluded since they
    have no connection to evaluate. Flights with trip_id == 0 (the sync
    fallback) or non-int trip_id are skipped.
    """
    by_trip: dict[int, list[dict]] = {}
    for state in flight_states:
        trip_id = state.get("trip_id")
        if not isinstance(trip_id, int) or isinstance(trip_id, bool) or trip_id == 0:
            continue
        by_trip.setdefault(trip_id, []).append(state)
    groups: list[list[dict]] = []
    for trip_id in sorted(by_trip):
        legs = by_trip[trip_id]
        if len(legs) < 2:
            continue
        legs_sorted = sorted(legs, key=lambda s: s.get("scheduled_dep_time") or "")
        groups.append(legs_sorted)
    return groups


def _detect_for_trip(
    *,
    trip_group: list[dict],
    now_utc: datetime,
    min_transfer_minutes: int,
) -> list[tuple[int, dict]]:
    """Walk one trip's sorted legs, emit events for tight connections."""
    events: list[tuple[int, dict]] = []
    for leg1, leg2 in zip(trip_group, trip_group[1:]):
        event = _evaluate_pair(
            leg1=leg1,
            leg2=leg2,
            now_utc=now_utc,
            min_transfer_minutes=min_transfer_minutes,
        )
        if event is not None:
            events.append((leg2["flight_id"], event))
    return events


def _evaluate_pair(
    *,
    leg1: dict,
    leg2: dict,
    now_utc: datetime,
    min_transfer_minutes: int,
) -> dict | None:
    """Return a connection_at_risk event for the (leg1, leg2) pair, or None."""
    if leg1.get("arr_airport_id") != leg2.get("dep_airport_id"):
        return None

    leg2_markers = leg2.get("phase_markers") or {}
    if leg2_markers.get("connection_at_risk_fired"):
        return None

    leg1_snapshot = leg1.get("last_snapshot") or {}
    leg1_status = leg1_snapshot.get("computed_status")
    if leg1_status in _LEG1_TERMINAL_STATUSES:
        return None

    leg1_scheduled_dep = _parse_iso8601(leg1.get("scheduled_dep_time"))
    if leg1_scheduled_dep is None:
        return None
    if leg1_scheduled_dep - now_utc > timedelta(hours=_LEG1_LOOKAHEAD_HOURS):
        return None

    projected_arr_str = leg1_snapshot.get("arr_time") or leg1.get("scheduled_arr_time")
    leg2_dep_str = leg2.get("scheduled_dep_time")
    if not projected_arr_str or not leg2_dep_str:
        return None

    projected_arr = _parse_iso8601(projected_arr_str)
    leg2_dep = _parse_iso8601(leg2_dep_str)
    if projected_arr is None or leg2_dep is None:
        return None

    transfer_minutes = int((leg2_dep - projected_arr).total_seconds() // 60)
    if transfer_minutes >= min_transfer_minutes:
        return None

    scheduled_arr = _parse_iso8601(leg1.get("scheduled_arr_time"))
    scheduled_layover_minutes: int | None
    if scheduled_arr is not None:
        scheduled_layover_minutes = int((leg2_dep - scheduled_arr).total_seconds() // 60)
    else:
        scheduled_layover_minutes = None

    return {
        "reason": "connection_at_risk",
        "leg1_code": leg1.get("code") or leg1_snapshot.get("code"),
        "leg2_code": leg2.get("code") or (leg2.get("last_snapshot") or {}).get("code"),
        "leg1_flight_id": leg1.get("flight_id"),
        "connecting_airport_id": leg2.get("dep_airport_id"),
        "transfer_minutes_remaining": transfer_minutes,
        "scheduled_layover_minutes": scheduled_layover_minutes,
        "min_transfer_minutes": min_transfer_minutes,
        "projected_leg1_arr_time": projected_arr_str,
        "scheduled_leg2_dep_time": leg2_dep_str,
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
