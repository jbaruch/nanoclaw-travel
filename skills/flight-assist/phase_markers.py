"""Time-based wake gates for the flight-assist precheck.

Where `wake_rules.py` detects events from snapshot deltas, this
module detects events from wall-clock time alone. Each marker fires
ONCE per flight; the once-fired flag lives in the per-flight state
record's `phase_markers` dict so the agent isn't notified twice
(e.g., a "leave by 11:30" alert that re-fires every cadence cycle).

Three time-based events:

- `day_before` — fires once `now ≥ scheduled_dep − 24h` (capability 2:
  day-before sanity check — agent composes a calendar-conflict + booking-
  diff message). A flight first seen inside that window (a post-misconnect
  rebook, a late add) fires at whatever T-minus it happens to be, often the
  same local day, so the payload carries the REAL `hours_until_dep` and a
  `day_label` (today / tomorrow / `YYYY-MM-DD`) resolved against the
  operator's local date by `day_label()` — the compose renders the label
  verbatim instead of doing date math off a constant (#300).
- `time_to_leave` — fires when `now + travel_time + buffer ≥
  scheduled_dep_time` (capability 1: traffic-aware leave-by alert),
  staying silent for boarding / taxi / pushback / gone, for fresh GPS
  at the departure airport, and for a missed leave-by unless the owner
  is freshly GPS-away and the snapshot is positively pre-departure (#349)
- `arrival_logistics` — fires at scheduled_arr_time − 15 min
  (capability 6: baggage carousel + Lyft + lounge prompts)

Each function returns `(should_fire, event_dict | None)`. The caller
(precheck.py) persists the notice before advancing its snapshot. Only a
confirmed send consumes the notice; later polls copy its receipt into
the marker flag. A wake alone never sets that flag.

Pure functions: no I/O, no state mutation. Travel time for the
time_to_leave gate comes in as an argument; the caller is the one
that queries `maps_client.travel_time()` per the cadence-ladder
budget.

stdlib-only: `datetime` per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import math
import re
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
# alone is not trustworthy (#54), and its detail can already read "Boarding now"
# while premature, so the label is also held against the planned boarding
# window (#295) wherever the caller knows the boarding lead.
_BOARDING_OR_GONE_STATUSES = frozenset({"departed", "en_route", "landed", "cancelled", "diverted"})
# Documented byAir pre-departure labels. Taxi-out / pushback are described in
# `computed_status_detail`, not as extra enum values (#349). A status string
# outside this set (and outside the gone set) is treated as not-pre-departure
# so an unpublished live value cannot re-open the leave-by gate.
_PRE_DEPARTURE_STATUSES = frozenset({"scheduled", "check_in_open", "boarding"})
# Taxi / pushback / gate-closed prose on the documented detail field. Independent
# of `computed_status` and of the #295 boarding window: a taxiing snapshot is
# already gone-phase for airport-bound prompts (#349).
_TAXI_OR_PUSHBACK_DETAIL = re.compile(
    r"\b(?:taxi(?:ing|ed|s)?|push(?:ing|ed)?[\s-]*back|pushback|gate[\s-]*closed)\b",
    re.IGNORECASE,
)
# Fresh GPS inside this radius of `depAirport.lat`/`lon` is presence at the
# departure airport. 3 km covers the terminal plus economy lots; downtown
# Nashville vs the live-sampled BNA fixture is outside it (#349).
AIRPORT_PRESENCE_RADIUS_KM = 3.0


def day_before_due(
    *,
    scheduled_dep_time: str | None,
    phase_markers: dict,
    now_utc: datetime,
) -> bool:
    """Whether `check_day_before` would fire now — the gate alone, no payload.

    The precheck asks this first and resolves the operator's zone only when it
    is True, so the zone reader never spawns for a flight whose `day_before`
    is not firing this cycle: already fired, unparseable departure, or more
    than `DAY_BEFORE_HOURS` out (#300).
    """
    if phase_markers.get("day_before_fired"):
        return False
    dep_dt = _parse_iso8601(scheduled_dep_time)
    if dep_dt is None:
        return False
    return now_utc >= dep_dt - timedelta(hours=DAY_BEFORE_HOURS)


def check_day_before(
    *,
    scheduled_dep_time: str | None,
    phase_markers: dict,
    now_utc: datetime,
    operator_tz: str | None = None,
) -> tuple[bool, dict | None]:
    """T-24h gate. Returns (should_fire, event_payload).

    `phase_markers["day_before_fired"]` must be False to fire; after
    confirmed delivery the caller sets it to True. `now_utc` must be timezone-aware
    UTC (callers use `datetime.now(timezone.utc)`).

    The gate is unchanged from v0.1; the payload is not. `hours_until_dep`
    is the real whole hours from `now_utc` to departure (24 at the exact
    threshold, 7 for a flight first tracked ~7h50m out, negative for one
    first seen after it left) — the old constant 24 read as "tomorrow" to
    the compose whatever the clock said (#300). `day_label` / `day_label_tz`
    come from `day_label()` below: `operator_tz` is the operator's current
    IANA zone as the core `current-tz` reader resolved it, or None when no
    zone is available; the caller resolves it, this function stays pure.
    """
    if not day_before_due(
        scheduled_dep_time=scheduled_dep_time, phase_markers=phase_markers, now_utc=now_utc
    ):
        return (False, None)
    dep_dt = _parse_iso8601(scheduled_dep_time)
    assert dep_dt is not None  # day_before_due returned True, so it parsed
    label, label_tz = day_label(dep_dt, now_utc=now_utc, operator_tz=operator_tz)
    return (
        True,
        {
            "reason": "day_before",
            "scheduled_dep_time": scheduled_dep_time,
            "hours_until_dep": int((dep_dt - now_utc).total_seconds() // 3600),
            "day_label": label,
            "day_label_tz": label_tz,
        },
    )


def day_label(
    dep_dt: datetime, *, now_utc: datetime, operator_tz: str | None
) -> tuple[str, str | None]:
    """The day word for a departure, resolved against the operator's local date.

    Returns `(label, zone)`. With a resolvable `operator_tz`, both instants
    are expressed in that zone and their calendar dates compared: the same
    date is `"today"`, the next is `"tomorrow"`, anything else is the
    operator-local ISO date (`YYYY-MM-DD`), and `zone` names the zone the
    comparison ran in. That is the comparison `rules/operator-local-tz-
    phrasing.md` prescribes, done here deterministically so the compose
    renders it instead of re-deriving it (#300).

    With no usable zone (`None`, or a name `ZoneInfo` cannot resolve) the
    label is the explicit date the departure carries in its OWN offset —
    `dep_dt` keeps the airport-local offset the RFC 3339 string had — and
    `zone` is None. The container's UTC date is never the fallback: it is
    the wrong date for hours around midnight, which is exactly when a
    relative word misleads. A zone name that does not resolve is reported
    here on stderr, naming the zone, and the fallback label still goes out.
    """
    if operator_tz:
        try:
            zone = ZoneInfo(operator_tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            print(
                f"phase_markers.day_label: operator zone {operator_tz!r} does not resolve "
                f"({exc}); labelling with the departure's own date — check the host "
                "tz_state row the core current-tz reader serves",
                file=sys.stderr,
            )
            zone = None
        if zone is not None:
            local_event = dep_dt.astimezone(zone).date()
            local_now = now_utc.astimezone(zone).date()
            if local_event == local_now:
                return "today", operator_tz
            if local_event == local_now + timedelta(days=1):
                return "tomorrow", operator_tz
            return local_event.isoformat(), operator_tz
    return dep_dt.date().isoformat(), None


def check_time_to_leave(
    *,
    scheduled_dep_time: str | None,
    travel_time_seconds: int | None,
    phase_markers: dict,
    now_utc: datetime,
    snapshot: dict | None = None,
    boarding_lead_minutes: int | None = None,
    at_airport: bool | None = None,
    prev_polled_at: datetime | None = None,
    prior_poll_interval_minutes: int = 0,
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

    `snapshot` is the current trimmed byAir snapshot. When `is_boarding_or_gone`
    reports the flight really boarding, taxiing / pushed back / gate-closed,
    or already gone — departed, en_route, landed, cancelled, or diverted
    (#102 / #349 — a delayed flight or a stale travel estimate can push the
    leave-by moment past boarding), the alert is moot and the gate stays
    silent rather than waking the agent to say nothing. Defaults to None so
    callers without a snapshot keep the pre-boarding behavior.

    `boarding_lead_minutes` is the flight's resolved boarding lead. When given,
    a "boarding" label before the planned boarding window (`scheduled_dep −
    lead`) is premature and does NOT suppress the alert (#295); None keeps the
    label-plus-detail predicate alone. Taxi / pushback detail still suppresses
    even when that window has slipped (#349).

    `at_airport` is the GPS presence verdict from `at_departure_airport`:
    True (fresh fix inside the airport radius) always suppresses; False
    (fresh fix clearly away) is required for every missed fire; None
    (absent, stale, or ambiguous location) is not proof of being away
    (#349).

    `prev_polled_at` is the last successful poll before this one and
    `prior_poll_interval_minutes` the cadence that poll scheduled the next
    one on (`precheck._interval_for` on the PRIOR snapshot; 0 when that
    status is not a documented pre-departure label). The timely notice is
    the first actual poll at or after `leave_by`: `prev_polled_at` is
    before `leave_by` and `now_utc` is within `prior_poll_interval_minutes`
    of `leave_by`, so a cadence that tightens or loosens on the new snapshot, an
    outage gap, or an unrecognised status cannot widen the window. It may
    fire without GPS. Every other fire is missed: it needs `at_airport is
    False` and a documented pre-departure status; empty, missing, or
    malformed status is not proof the flight has not left. With no
    `prev_polled_at` (a pure call, or a flight never polled before) the
    timely window is the exact `leave_by` instant.

    `phase_markers["time_to_leave_fired"]` must be False to fire.
    """
    if phase_markers.get("time_to_leave_fired"):
        return (False, None)
    window_open = (
        None
        if boarding_lead_minutes is None
        else boarding_window_open(
            scheduled_dep_time=scheduled_dep_time,
            boarding_lead_minutes=boarding_lead_minutes,
            snapshot=snapshot,
        )
    )
    if is_boarding_or_gone(snapshot, boarding_window_open=window_open, at=now_utc):
        return (False, None)
    if at_airport is True:
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
    first_poll_after_leave_by = prev_polled_at is not None and prev_polled_at < leave_by
    timely_window = timedelta(
        minutes=max(prior_poll_interval_minutes, 0) if first_poll_after_leave_by else 0
    )
    missed = now_utc > leave_by + timely_window
    if missed:
        if at_airport is not False:
            return (False, None)
        if not positively_pre_departure(snapshot):
            return (False, None)
    return (
        True,
        {
            "reason": "time_to_leave",
            "leave_by": leave_by.isoformat(),
            "travel_time_minutes": travel_time_seconds // 60,
            "scheduled_dep_time": scheduled_dep_time,
            "missed": missed,
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


def boarding_window_open(
    *,
    scheduled_dep_time: str | None,
    boarding_lead_minutes: int,
    snapshot: dict | None = None,
) -> datetime | None:
    """The instant boarding is planned to begin, or None if no departure parses.

    `effective_dep − boarding_lead` — the start of the boarding calendar block
    flight-assist itself creates. The effective departure is byAir's live
    `dep_time` when the snapshot carries a parseable one, else the scheduled
    time: the same preference `calendar_reconcile._effective_times` gives the
    block, so a delayed or revised departure moves the block and this window
    together and byAir's early "boarding" flip on a delayed flight is still
    held to the block's own start. A present-but-unparseable `dep_time` falls
    back to the scheduled time here (the block planner surfaces it instead) —
    a wake gate with no window at all would let the premature label through.
    byAir claiming "boarding" before this instant is premature by
    construction; `wake_rules.is_real_boarding` holds the label against it
    (#295).
    """
    live = (snapshot or {}).get("dep_time")
    dep_dt = _parse_iso8601(live) if isinstance(live, str) else None
    if dep_dt is None:
        dep_dt = _parse_iso8601(scheduled_dep_time)
    if dep_dt is None:
        return None
    return dep_dt - timedelta(minutes=boarding_lead_minutes)


def gate_assignment_window_open(
    *,
    scheduled_dep_time: str | None,
    boarding_lead_minutes: int,
) -> datetime | None:
    """The instant the gate-readout window opens, or None if dep time is unparseable.

    `scheduled_dep − boarding_lead − GATE_ASSIGNMENT_WINDOW_LEAD_MINUTES`. The
    readout (`check_gate_assignment`) only fires once now is at/after this
    boundary (#103).
    """
    boarding_open = boarding_window_open(
        scheduled_dep_time=scheduled_dep_time, boarding_lead_minutes=boarding_lead_minutes
    )
    if boarding_open is None:
        return None
    return boarding_open - timedelta(minutes=GATE_ASSIGNMENT_WINDOW_LEAD_MINUTES)


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

    A flight `is_boarding_or_gone` — really boarding, or departed/en_route/
    landed/cancelled/diverted — gets no readout; navigating to a departure gate
    is moot by then (same gate as the leave-by suppression in #102). "Really
    boarding" is held against the planned boarding window derived from the
    same `boarding_lead_minutes` (#295), so byAir's premature label does not
    swallow the readout.

    `phase_markers["gate_assignment_fired"]` must be False to fire; the
    caller sets it True once fired so subsequent gate moves surface as
    ordinary `gate_change` events.
    """
    if phase_markers.get("gate_assignment_fired"):
        return (False, None)
    boarding_open = boarding_window_open(
        scheduled_dep_time=scheduled_dep_time,
        boarding_lead_minutes=boarding_lead_minutes,
        snapshot=snapshot,
    )
    if is_boarding_or_gone(snapshot, boarding_window_open=boarding_open, at=now_utc):
        return (False, None)
    window_open = gate_assignment_window_open(
        scheduled_dep_time=scheduled_dep_time,
        boarding_lead_minutes=boarding_lead_minutes,
    )
    if window_open is None or now_utc < window_open:
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


def is_boarding_or_gone(
    snapshot: dict | None,
    *,
    boarding_window_open: datetime | None = None,
    at: datetime | None = None,
) -> bool:
    """True when an airport-bound prompt no longer makes sense for this flight.

    The flight is really boarding (per `wake_rules.is_real_boarding`, which
    screens out byAir's premature "boarding" label), taxiing / pushed back /
    gate-closed per `computed_status_detail`, already gone, or carrying a
    status outside the documented pre-departure set. Either way the user is
    at — or past — the gate, so neither the leave-by gate (#102 / #349) nor
    the gate/terminal readout (#103) should fire.

    Taxi / pushback / gate-closed detail is independent of `computed_status`
    and of the #295 window: a delayed taxi-out that slips the boarding
    window still suppresses leave-by.

    `boarding_window_open` / `at` pass straight through to `is_real_boarding`:
    with both, a "boarding" label polled before the planned boarding window is
    premature and does not count (#295); without, the label-plus-detail
    predicate alone decides. Undocumented status values are not added to a
    live enum; they are simply not pre-departure.
    """
    if not snapshot:
        return False
    if is_real_boarding(snapshot, boarding_window_open=boarding_window_open, at=at):
        return True
    if _taxi_or_pushback_detail(snapshot):
        return True
    status = snapshot.get("computed_status")
    if status in _BOARDING_OR_GONE_STATUSES:
        return True
    if isinstance(status, str) and status.strip() and status not in _PRE_DEPARTURE_STATUSES:
        return True
    return False


def _taxi_or_pushback_detail(snapshot: dict) -> bool:
    detail = snapshot.get("computed_status_detail")
    if not isinstance(detail, str):
        return False
    return _TAXI_OR_PUSHBACK_DETAIL.search(detail) is not None


def positively_pre_departure(snapshot: dict | None) -> bool:
    """True when `computed_status` is a documented pre-departure label.

    Empty, missing, or non-string status is not proof the flight has
    not left, so a missed leave-by stays silent (#349).
    """
    if not snapshot:
        return False
    status = snapshot.get("computed_status")
    return isinstance(status, str) and status in _PRE_DEPARTURE_STATUSES


def departure_airport_coords(dep_airport: object) -> tuple[float | None, float | None]:
    """`lat` / `lon` off a byAir `get_airport` payload, or (None, None).

    Same keys as the live-sampled `BNA_AIRPORT` fixture. Nested
    `get_flight.depAirport` is not assumed to carry coordinates; the
    precheck resolves them from `get_airport(depAirport.id)` once per
    flight and caches `dep_lat` / `dep_lon` on `last_snapshot` (#349).
    """
    if not isinstance(dep_airport, dict):
        return None, None
    return _as_coordinate(dep_airport.get("lat"), 90.0), _as_coordinate(
        dep_airport.get("lon"), 180.0
    )


def at_departure_airport(
    *,
    origin_lat: float | None,
    origin_lon: float | None,
    dep_lat: float | None,
    dep_lon: float | None,
) -> bool | None:
    """Presence of a live GPS fix relative to the departure airport.

    True when the fix is inside `AIRPORT_PRESENCE_RADIUS_KM`. False when it is
    clearly outside. None when any coordinate is missing — that is ambiguous,
    not proof of being away (#349).
    """
    if origin_lat is None or origin_lon is None or dep_lat is None or dep_lon is None:
        return None
    return _haversine_km(origin_lat, origin_lon, dep_lat, dep_lon) <= AIRPORT_PRESENCE_RADIUS_KM


def _as_coordinate(value: object, limit: float) -> float | None:
    """A finite number within ±`limit` as a float, else None."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not -limit <= value <= limit or not math.isfinite(value):
        return None
    return float(value)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _parse_iso8601(value: str | None) -> datetime | None:
    """Parse an RFC3339 / ISO8601 string into a timezone-aware datetime.

    Returns None on malformed input. A trailing `Z` (RFC3339 zulu) is
    normalized to `+00:00` first — `datetime.fromisoformat` rejects `Z` on
    Python < 3.11, and scheduled times can come back zulu-suffixed, so without
    this the time-based markers would silently never fire (matches
    `precheck._parse_iso8601` / `state._parse_iso8601`). Naive datetimes (no
    tzinfo) are treated as UTC so a malformed-but-parseable value doesn't
    silently skew the comparison.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
