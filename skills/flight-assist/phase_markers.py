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
# The gate/terminal readout window opens this many minutes before boarding
# begins (boarding = scheduled_dep − boarding_lead). Gate info earlier than
# this is recorded to state silently; the readout is the first in-window
# notification (#103).
GATE_ASSIGNMENT_WINDOW_LEAD_MINUTES = 60

# Statuses at/after which an airport-bound prompt — "leave for the airport now"
# (#102) or "head to terminal X" (#103) — is moot: the flight has already left
# or won't go. Real boarding is detected separately via wake_rules.is_real_boarding;
# byAir flips computed_status to "boarding" up to ~1h early, so the raw label
# alone is not trustworthy (#54).
_BOARDING_OR_GONE_STATUSES = frozenset({"departed", "en_route", "landed", "cancelled", "diverted"})


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
    if _boarding_or_gone(snapshot):
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


def check_gate_assignment(
    *,
    scheduled_dep_time: str | None,
    boarding_lead_minutes: int,
    snapshot: dict | None,
    phase_markers: dict,
    now_utc: datetime,
) -> tuple[bool, dict | None]:
    """Once-per-flight gate + terminal readout. Returns (should_fire, payload).

    The window opens at `scheduled_dep − boarding_lead − 1h`. The readout is
    the first in-window cycle a departure gate exists: it carries the
    departure gate + terminal so the operator knows which terminal to head
    to. When no gate is assigned yet as the window opens (late gate
    assignment is common), the readout defers to the first in-window cycle a
    gate appears. Gate info before the window is recorded to state silently
    by the caller and never fires here (#103).

    A flight already boarding, departed, cancelled, or diverted gets no
    readout — navigating to a departure gate is moot by then (same gate as
    the leave-by suppression in #102).

    `phase_markers["gate_assignment_fired"]` must be False to fire; the
    caller sets it True once fired so subsequent gate moves surface as
    ordinary `gate_change` events.
    """
    if phase_markers.get("gate_assignment_fired"):
        return (False, None)
    if _boarding_or_gone(snapshot):
        return (False, None)
    dep_dt = _parse_iso8601(scheduled_dep_time)
    if dep_dt is None:
        return (False, None)
    window_open = (
        dep_dt
        - timedelta(minutes=boarding_lead_minutes)
        - timedelta(minutes=GATE_ASSIGNMENT_WINDOW_LEAD_MINUTES)
    )
    if now_utc < window_open:
        return (False, None)
    if not snapshot:
        return (False, None)
    dep_gate = snapshot.get("dep_gate")
    if dep_gate is None:
        return (False, None)
    return (
        True,
        {
            "reason": "gate_assignment",
            "dep_gate": dep_gate,
            "dep_terminal": snapshot.get("dep_terminal"),
        },
    )


def _boarding_or_gone(snapshot: dict | None) -> bool:
    """True when an airport-bound prompt no longer makes sense for this flight.

    The flight is either really boarding (per `wake_rules.is_real_boarding`,
    which screens out byAir's premature "boarding" label) or its status has
    moved past departure (or it won't go). Either way the user is at — or past —
    the gate, so neither the leave-by gate (#102) nor the gate/terminal readout
    (#103) should fire.
    """
    if not snapshot:
        return False
    if is_real_boarding(snapshot):
        return True
    return snapshot.get("computed_status") in _BOARDING_OR_GONE_STATUSES


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
