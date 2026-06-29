"""Time-based wake gates for the flight-assist precheck.

Where `wake_rules.py` detects events from snapshot deltas, this
module detects events from wall-clock time alone. Each marker fires
ONCE per flight; the once-fired flag lives in the per-flight state
record's `phase_markers` dict so the agent isn't notified twice
(e.g., a "leave by 11:30" alert that re-fires every cadence cycle).

Three time-based events:

- `day_before` — fires at T-24h before scheduled departure (capability
  2: day-before sanity check — agent composes a calendar-conflict
  + booking-diff message)
- `time_to_leave` — fires when `now + travel_time + buffer ≥
  scheduled_dep_time` (capability 1: traffic-aware leave-by alert)
- `arrival_logistics` — fires at scheduled_arr_time − 15 min
  (capability 6: baggage carousel + Lyft + lounge prompts)

Each function returns `(should_fire, event_dict | None)`. The caller
(precheck.py) is responsible for setting the marker flag in state
after firing so subsequent cycles don't re-emit.

Pure functions: no I/O, no state mutation. Travel time for the
time_to_leave gate comes in as an argument; the caller is the one
that queries `maps_client.travel_time()` per the cadence-ladder
budget.

stdlib-only: `datetime` per `coding-policy: dependency-management`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wake_rules import is_real_boarding

DAY_BEFORE_HOURS = 24
TIME_TO_LEAVE_BUFFER_MINUTES = 15
ARRIVAL_LOGISTICS_LEAD_MINUTES = 15

# Statuses at/after which a "leave for the airport now" alert is moot — the
# flight has already left (or won't go). Real boarding is detected separately
# via wake_rules.is_real_boarding; byAir flips computed_status to "boarding"
# up to ~1h early, so the raw label alone is not trustworthy (#54).
_LEAVE_ALERT_MOOT_STATUSES = frozenset({"departed", "en_route", "landed", "cancelled", "diverted"})


def check_day_before(
    *,
    scheduled_dep_time: str | None,
    phase_markers: dict,
    now_utc: datetime,
) -> tuple[bool, dict | None]:
    """T-24h gate. Returns (should_fire, event_payload).

    `phase_markers["day_before_fired"]` must be False to fire; once
    fired the caller sets it to True. `now_utc` must be timezone-aware
    UTC (callers use `datetime.now(timezone.utc)`).
    """
    if phase_markers.get("day_before_fired"):
        return (False, None)
    dep_dt = _parse_iso8601(scheduled_dep_time)
    if dep_dt is None:
        return (False, None)
    threshold = dep_dt - timedelta(hours=DAY_BEFORE_HOURS)
    if now_utc < threshold:
        return (False, None)
    return (
        True,
        {
            "reason": "day_before",
            "scheduled_dep_time": scheduled_dep_time,
            "hours_until_dep": DAY_BEFORE_HOURS,
        },
    )


def check_time_to_leave(
    *,
    scheduled_dep_time: str | None,
    travel_time_seconds: int | None,
    phase_markers: dict,
    now_utc: datetime,
    snapshot: dict | None = None,
) -> tuple[bool, dict | None]:
    """Traffic-aware "leave by" gate. Returns (should_fire, event_payload).

    Fires when `now + travel_time + buffer ≥ scheduled_dep_time`, i.e.,
    the user must leave now (or already-late) to make the flight given
    current traffic.

    `travel_time_seconds` is the in-traffic value from
    `maps_client.travel_time(...).in_traffic_seconds`. If None
    (maps API didn't return a traffic estimate or the caller didn't
    query maps yet), the gate doesn't fire — the caller defers the
    decision until traffic data is available.

    `snapshot` is the current trimmed byAir snapshot. When it shows the
    flight already boarding or departed (#102 — a delayed flight or a
    stale travel estimate can push the leave-by moment past boarding),
    the alert is moot and the gate stays silent rather than waking the
    agent to say nothing. Defaults to None so callers without a snapshot
    keep the pre-boarding behavior.

    `phase_markers["time_to_leave_fired"]` must be False to fire.
    """
    if phase_markers.get("time_to_leave_fired"):
        return (False, None)
    if _leave_alert_moot(snapshot):
        return (False, None)
    if travel_time_seconds is None:
        return (False, None)
    dep_dt = _parse_iso8601(scheduled_dep_time)
    if dep_dt is None:
        return (False, None)
    buffer = timedelta(minutes=TIME_TO_LEAVE_BUFFER_MINUTES)
    travel = timedelta(seconds=travel_time_seconds)
    leave_by = dep_dt - travel - buffer
    if now_utc < leave_by:
        return (False, None)
    return (
        True,
        {
            "reason": "time_to_leave",
            "leave_by": leave_by.isoformat(),
            "travel_time_minutes": travel_time_seconds // 60,
            "scheduled_dep_time": scheduled_dep_time,
        },
    )


def check_arrival_logistics(
    *,
    scheduled_arr_time: str | None,
    phase_markers: dict,
    now_utc: datetime,
) -> tuple[bool, dict | None]:
    """T-arr-15min gate. Returns (should_fire, event_payload).

    Fires 15 minutes before scheduled arrival so the agent can surface
    baggage carousel (from the snapshot, populated by then or not),
    Lyft estimate, and lounge prompts if transit.

    `phase_markers["arrival_logistics_fired"]` must be False to fire.
    """
    if phase_markers.get("arrival_logistics_fired"):
        return (False, None)
    arr_dt = _parse_iso8601(scheduled_arr_time)
    if arr_dt is None:
        return (False, None)
    threshold = arr_dt - timedelta(minutes=ARRIVAL_LOGISTICS_LEAD_MINUTES)
    if now_utc < threshold:
        return (False, None)
    return (
        True,
        {
            "reason": "arrival_logistics",
            "scheduled_arr_time": scheduled_arr_time,
            "minutes_until_arr": ARRIVAL_LOGISTICS_LEAD_MINUTES,
        },
    )


def _leave_alert_moot(snapshot: dict | None) -> bool:
    """True when a "leave for the airport" alert no longer makes sense.

    The flight is either really boarding (per `wake_rules.is_real_boarding`,
    which screens out byAir's premature "boarding" label) or its status has
    moved past departure. Either way the user is at — or past — the gate, so
    the leave-by gate must not fire (#102).
    """
    if not snapshot:
        return False
    if is_real_boarding(snapshot):
        return True
    return snapshot.get("computed_status") in _LEAVE_ALERT_MOOT_STATUSES


def _parse_iso8601(value: str | None) -> datetime | None:
    """Parse an RFC3339 / ISO8601 string into a timezone-aware datetime.

    Returns None on malformed input. Naive datetimes (no tzinfo) are
    treated as UTC so a malformed-but-parseable value doesn't silently
    skew the comparison.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
