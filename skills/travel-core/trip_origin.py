"""Trip-aware drive-anchor resolution — TripIt truth over the static home.

Every drive leg the travel skills plan is anchored somewhere: the sweep's
outbound/return legs at the operator's residence, flight-assist's
time-to-leave and drive-home legs at `home_address`. That anchor was always
the static home, with no awareness of whether the operator is on a trip —
which is how a UK dinner reservation drew a 39-minute drive block from a
Tennessee origin (issue #122). This module resolves the anchor from the
TripIt-derived `travel-schedule.json` (written nightly by
`nightly-travel-sync`'s refresh-travel-schedule.py):

1. No active `Trip` segment covers the anchor time → the static home
   (today's behavior, unchanged).
2. An active `Trip` covers it → the `location` of the most recent `Lodging`
   event (check-in OR check-out) within the trip's span at or before the
   anchor time. In a check-out→check-in gap the latest event is the prior
   check-out, so its lodging wins; after the next check-in, that lodging
   wins. The event's `location` field carries the address (`address` is
   null in the feed).
3. On a trip but before its first lodging event → the `Trip` segment's own
   `location` when present, else unresolved (`address=None`) — the caller
   surfaces "no drivable origin" instead of planning from home. The static
   home also anchors home-metro trips without lodging (#314).

Rule 2/3 apply only from the moment the operator has actually left, though: the
date-only `Trip` wrapper is "active" on the departure day itself, but before the
trip's first flight departs the operator is still home. Anchoring the outbound
airport-departure drive at the trip's destination there draws an absurd
cross-country "drive" (a 34-hour San Francisco→BNA block for a BNA→SFO trip), so
the static home wins until the first flight lifts off. A trip with no timed
flight in the feed keeps the old behavior — nothing marks when it left home.

The schedule file is host-group state owned by `nightly-travel-sync` (see
its state-schema.md); this module is a non-owner READER per
`coding-policy: stateful-artifacts` — a missing, unreadable, malformed, or
forward-incompatible file resolves to "no usable schedule" (static-home
behavior), never an exception, and never a migration.

Shared across bundles: drive-engine's reconcile sweep imports this module
cross-bundle the same way it already imports `maps_client` from flight-assist.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from trip_origin import (
        TripAnchor, flight_summaries, flight_windows, load_travel_schedule,
        resolve_anchor,
    )

    schedule = load_travel_schedule()            # list | None, tolerant
    anchor = resolve_anchor(schedule, at=meeting_start, home_address=home)
    anchor.address    # drivable anchor, or None (unresolved mid-trip)
    anchor.source     # "home" | "lodging" | "trip_location" | "unresolved"
    flight_windows(schedule)    # [(start, end), ...] — flight spans to filter (#85)
    flight_summaries(schedule)  # ["DL 4908 ...", ...] — flight identities to filter (#85)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Co-located in this bundle; import by module name so consumers that put the
# bundle on sys.path (see SKILL.md) resolve it the same way they resolve this
# module.
_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from addresses import home_metro_names, is_home_metro  # noqa: E402
from lodging import CHECK_IN, lodging_role  # noqa: E402

SCHEDULE_PATH = "/workspace/group/travel-schedule.json"

# Highest travel-schedule.json record schema this reader accepts. Bump in
# lock-step with refresh-travel-schedule.py's SCHEMA_VERSION per
# `coding-policy: stateful-artifacts`. Records without a schema_version are
# legacy pre-versioned records (written before the field existed) that this
# reader treats as v1; any record carrying a HIGHER version marks the whole
# file forward-incompatible — this reader is lagging, so it takes the
# no-usable-schedule path rather than guessing at a shape it doesn't know.
SCHEDULE_SCHEMA_VERSION = 5


@dataclass(frozen=True)
class TripAnchor:
    """Where home-anchored drive legs start/end at a given moment.

    Fields:
        address: the drivable anchor (static home off-trip, lodging or trip
            location on-trip), or None when on a trip with nothing resolvable
            — the caller must surface that, not fall back to home.
        source: which rule produced the address — "home", "lodging",
            "trip_location", or "unresolved" (address is None).
        detail: human-readable context (the lodging event or trip summary,
            or the reason nothing resolved) for diagnostics and operator
            messaging.
    """

    address: str | None
    source: str
    detail: str | None = None


def load_travel_schedule(path: str | None = None) -> list[dict] | None:
    """Read travel-schedule.json, or None when no usable schedule exists.

    None (missing / unreadable / malformed / non-list root /
    forward-incompatible record version) means "resolve anchors as if not
    traveling" — the pre-#122 static-home behavior. That degraded mode is
    deliberate: the schedule's own alerting surface is `nightly-travel-sync`
    (freshness probe + failure branch), so a broken file must not take the
    drive planners down with it. A stderr diagnostic records the cause.
    """
    schedule_path = Path(path if path is not None else SCHEDULE_PATH)
    try:
        payload = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # OSError covers missing + unreadable; UnicodeDecodeError a
        # non-UTF-8 half-write; JSONDecodeError a truncated refresh.
        print(
            f"trip_origin: no usable travel schedule at {schedule_path} "
            f"({type(exc).__name__}) — resolving drive anchors as not traveling",
            file=sys.stderr,
        )
        return None
    if not isinstance(payload, list):
        print(
            f"trip_origin: travel schedule at {schedule_path} has a non-list "
            "root — resolving drive anchors as not traveling",
            file=sys.stderr,
        )
        return None
    records = [record for record in payload if isinstance(record, dict)]
    for record in records:
        version = record.get("schema_version")
        if version is None:
            continue  # legacy pre-versioned record — read as v1
        if not isinstance(version, int) or isinstance(version, bool):
            continue  # malformed version on one record — the record set still reads
        if version > SCHEDULE_SCHEMA_VERSION:
            print(
                f"trip_origin: travel schedule carries schema_version={version} "
                f"(this reader supports v{SCHEDULE_SCHEMA_VERSION}) — resolving "
                "drive anchors as not traveling until the plugin is upgraded",
                file=sys.stderr,
            )
            return None
    return records


def parse_schedule_time(value) -> datetime | None:
    """A schedule `start`/`end` string as a tz-aware UTC datetime, else None.

    The feed emits `YYYY-MM-DDTHH:MM:SSZ` for timed VEVENTs and `YYYY-MM-DD`
    for date-only wrappers (see refresh-travel-schedule.py); a date-only
    value reads as midnight UTC. A naive datetime string (not a shape the
    feed writes) is tolerated as UTC rather than rejected.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_day(value) -> date | None:
    """A schedule `start`/`end` string as a UTC calendar date, else None."""
    parsed = parse_schedule_time(value)
    return parsed.date() if parsed is not None else None


def _active_trip(records: list[dict], on_day: date) -> dict | None:
    """The `Trip` record whose date span covers `on_day`, else None.

    Trip wrappers are date-only; the span is inclusive of both endpoint
    dates. TripIt's date-only DTEND is nominally exclusive, so inclusive
    reading may extend trip awareness one day past the return — the safe
    direction: anchoring a landing-day evening at the last lodging beats
    planning a mid-trip drive from home, which is the failure #122 exists
    to stop. Multiple covering trips (overlapping wrappers) resolve to the
    latest-starting one.
    """
    active = None
    active_start = None
    for record in records:
        if record.get("type") != "Trip":
            continue
        start_day = _parse_day(record.get("start"))
        end_day = _parse_day(record.get("end"))
        if start_day is None or end_day is None:
            continue
        if start_day <= on_day <= end_day and (active_start is None or start_day > active_start):
            active = record
            active_start = start_day
    return active


# Record types whose departure marks the operator physically leaving for a
# trip. Rail belongs beside Flight: a train out is a departure from home just as
# much as a flight is.
_DEPARTURE_TYPES = frozenset({"Flight", "Rail"})

# How far in front of a trip's first transport departure a lodging check-in can
# sit and still be a STAGING stay — an airport hotel the operator drove to from
# home the night before, so the journey still opened at the house.
#
# The separator `opened_from_home` needs is staging-vs-destination, and without
# geography the lead time is the signal there is: an overnight before an early
# flight runs hours, while a stay the operator drove to and later flew a local
# round trip out of runs days. A day covers the overnight case with room for a
# long check-in-to-departure gap, and is well short of any destination stay.
STAGING_STAY_MAX_LEAD = timedelta(hours=24)


def _timed_within(record: dict, trip_start: date | None, trip_end: date | None) -> datetime | None:
    """A record's `start` as a UTC instant, if it is TIMED and inside the span.

    Timed only. A date-only `YYYY-MM-DD` start parses to midnight, which would
    falsely mark the trip as already begun for the rest of that day and let a
    same-day anchor fall through to the destination — the very bug the caller's
    gate closes. Mirrors `flight_windows`.
    """
    raw_start = record.get("start")
    if not (isinstance(raw_start, str) and "T" in raw_start):
        return None
    when = parse_schedule_time(raw_start)
    if when is None:
        return None
    if trip_start is not None and trip_end is not None:
        if not (trip_start <= when.date() <= trip_end):
            return None
    return when


def _first_transport_departure(
    records: list[dict], trip_start: date | None, trip_end: date | None
) -> datetime | None:
    """The earliest timed transport departure within the trip's span, else None."""
    earliest: datetime | None = None
    for record in records:
        if record.get("type") not in _DEPARTURE_TYPES:
            continue
        when = _timed_within(record, trip_start, trip_end)
        if when is not None and (earliest is None or when < earliest):
            earliest = when
    return earliest


def _first_lodging_arrival(
    records: list[dict], trip_start: date | None, trip_end: date | None
) -> datetime | None:
    """The earliest timed lodging check-in within the trip's span, else None.

    Only a check-in with a usable location counts — the lodging ladder can
    anchor on nothing else, so treating a blank-location check-in as the trip's
    start would step past the caller's gate and land on the destination city,
    which is the shape the gate exists to prevent.
    """
    earliest: datetime | None = None
    for record in records:
        if record.get("type") != "Lodging":
            continue
        if lodging_role(record.get("summary")) != CHECK_IN:
            continue
        location = record.get("location")
        if not isinstance(location, str) or not location.strip():
            continue
        when = _timed_within(record, trip_start, trip_end)
        if when is not None and (earliest is None or when < earliest):
            earliest = when
    return earliest


def _trip_begins_at(
    records: list[dict], trip_start: date | None, trip_end: date | None
) -> datetime | None:
    """When the operator physically leaves home for the trip, else None.

    The EARLIEST of the trip's first timed transport departure and its first
    timed lodging check-in. Before that instant the date-only Trip wrapper is
    already "active" but the planned position is still home.

    Each half covers a case the other misses:

    - Transport alone left a FLIGHT-LESS drive trip with no begin instant at
      all, so the gate never fired and a first-day anchor before check-in
      resolved to the Trip wrapper's own `location` — the destination city, the
      cross-country-drive shape the gate exists to prevent (#233).
    - Transport FIRST rather than earliest missed the mirror case: an airport
      hotel checked into the night before an early flight. The operator slept
      there, but every instant before wheels-up read as home, so the morning
      airport drive routed from the house he had already left (#235, the #154
      shape reintroduced by this gate).

    A trip with neither signal yields None and the caller leaves the anchor as
    it was.
    """
    candidates = [
        when
        for when in (
            _first_transport_departure(records, trip_start, trip_end),
            _first_lodging_arrival(records, trip_start, trip_end),
        )
        if when is not None
    ]
    return min(candidates) if candidates else None


def resolve_anchor(
    schedule: list[dict] | None,
    *,
    at: datetime,
    home_address: str | None,
    home_metros: frozenset[str] = frozenset(),
) -> TripAnchor:
    """Resolve the drive anchor for time `at` per the #122 rules. Pure.

    Args:
        schedule: the record list from `load_travel_schedule` (None means
            no usable schedule — anchor at home).
        at: the tz-aware moment the anchor applies to (a meeting start, or
            "now" for flight-assist's cycle origin). Naive raises ValueError
            — comparing it to the schedule's UTC instants would be wrong,
            not just an exception.
        home_address: the static residence used off-trip. May be None
            (flight-assist's config leaves it unset), in which case the
            off-trip anchor is None with source "home" — same "no origin
            configured" contract callers already handle.
        home_metros: normalized labels from `addresses.home_metro_names`.
            A matching trip without lodging resolves to home (#314).

    Returns:
        TripAnchor — see the class docstring for the source ladder.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("resolve_anchor: `at` must be timezone-aware (UTC)")
    if not schedule:
        return TripAnchor(address=home_address, source="home")

    at_utc = at.astimezone(timezone.utc)
    trip = _active_trip(schedule, at_utc.date())
    if trip is None:
        return TripAnchor(address=home_address, source="home")

    trip_start = _parse_day(trip.get("start"))
    trip_end = _parse_day(trip.get("end"))

    # A local placeholder is still home unless its span contains a lodging
    # reservation. Count future and address-less lodging too: neither proves
    # that this is a placeholder (#314).
    has_lodging = any(
        record.get("type") == "Lodging"
        and (day := _parse_day(record.get("start"))) is not None
        and trip_start is not None
        and trip_end is not None
        and trip_start <= day <= trip_end
        for record in schedule
    )
    if is_home_metro(trip.get("location"), home_metros) and not has_lodging:
        return TripAnchor(
            address=home_address, source="home", detail="home-metro trip without lodging"
        )

    # Before the trip begins the operator is still home — the date-only Trip
    # wrapper is "active" on the first day, but anchoring an outbound drive at
    # the destination draws a cross-country "drive" (the 34-hour San
    # Francisco→BNA block for a BNA→SFO trip). "Begins" is the first transport
    # departure, falling back to the first lodging check-in when the trip has
    # none, so a flight-less drive trip is gated too (`_trip_begins_at`, #233).
    # Transport still wins whenever it exists: a pre-flight staging hotel does
    # NOT begin the trip, and the morning of an early flight still reads home
    # (#235).
    begins_at = _trip_begins_at(schedule, trip_start, trip_end)
    if begins_at is not None and at_utc < begins_at:
        return TripAnchor(
            address=home_address,
            source="home",
            detail="before the trip begins",
        )

    best = None
    best_when = None
    for record in schedule:
        if record.get("type") != "Lodging":
            continue
        location = record.get("location")
        if not isinstance(location, str) or not location.strip():
            continue
        when = parse_schedule_time(record.get("start"))
        if when is None or when > at_utc:
            continue
        # Bound lodging to the active trip's span so a prior trip's
        # straggler check-out (retained by the refresh's live-stay pairing)
        # can't anchor this trip's meetings in the wrong city.
        if trip_start is not None and trip_end is not None:
            if not (trip_start <= when.date() <= trip_end):
                continue
        if best_when is None or when >= best_when:
            best = record
            best_when = when
    if best is not None:
        location = best.get("location")
        assert isinstance(location, str)  # filtered non-str/empty above
        return TripAnchor(
            address=location.strip(),
            source="lodging",
            detail=best.get("summary") or None,
        )

    trip_location = trip.get("location")
    trip_summary = trip.get("summary") or "active trip"
    if isinstance(trip_location, str) and trip_location.strip():
        return TripAnchor(
            address=trip_location.strip(),
            source="trip_location",
            detail=trip_summary,
        )
    return TripAnchor(
        address=None,
        source="unresolved",
        detail=(
            f"on {trip_summary!r} with no lodging event at or before "
            f"{at_utc.isoformat()} and no trip location — no drivable anchor"
        ),
    )


def _flight_records(schedule: list[dict] | None) -> list[dict]:
    """The `Flight`-type records from the schedule (dicts only), else empty."""
    if not schedule:
        return []
    return [r for r in schedule if isinstance(r, dict) and r.get("type") == "Flight"]


def flight_windows(schedule: list[dict] | None) -> list[tuple[datetime, datetime]]:
    """UTC (start, end) spans for every timed `Flight` segment in the schedule.

    drive-engine's `scan` uses these to filter TripIt flight events out of
    ground-meeting classification (#85): a calendar event overlapping a flight
    window is air travel — owned by flight-assist — never a ground meeting to
    draw a drive block for (the London-hotel→JFK-layover "drive"). A None /
    empty schedule yields no windows, so this time-overlap signal goes quiet;
    `scan` still applies its intrinsic flight-template summary rule, and a real
    (non-flight) meeting is never suppressed by an absent schedule.

    Only a segment whose `start` and `end` both parse to instants AND both
    carry a time-of-day (`T` in the raw value) produces a window. A date-only
    or unparseable segment is skipped: a date-only "flight" would span whole
    calendar days and could suppress a real same-day meeting, so the safe
    direction is to emit no window for it. A non-positive span is skipped too.

    The time-overlap match this feeds is defeated by a duplicate flight event
    whose timezone is corrupted (its span misses the true window); `scan`
    pairs these windows with a schedule-independent summary-template match and
    the `flight_summaries` code match below to catch those too.
    """
    windows: list[tuple[datetime, datetime]] = []
    for record in _flight_records(schedule):
        raw_start = record.get("start")
        raw_end = record.get("end")
        if not (isinstance(raw_start, str) and "T" in raw_start):
            continue
        if not (isinstance(raw_end, str) and "T" in raw_end):
            continue
        start = parse_schedule_time(raw_start)
        end = parse_schedule_time(raw_end)
        if start is None or end is None or end <= start:
            continue
        windows.append((start, end))
    return windows


def flight_summaries(schedule: list[dict] | None) -> list[str]:
    """Summaries of every `Flight` segment in the schedule (non-empty strings).

    `scan` extracts IATA flight designators (e.g. "DL 4908") from these to
    match a calendar flight event to a scheduled flight by identity, catching
    duplicate flight events whose corrupted times miss the `flight_windows`
    overlap (#85 follow-up). Unlike the windows, this ignores the segment's
    time entirely — identity, not instant. A None / empty schedule yields no
    summaries.
    """
    summaries: list[str] = []
    for record in _flight_records(schedule):
        summary = record.get("summary")
        if isinstance(summary, str) and summary.strip():
            summaries.append(summary)
    return summaries


def opened_from_home(
    schedule: list[dict] | None,
    *,
    at: datetime,
    home_address: str | None,
    home_metros: frozenset[str] = frozenset(),
) -> bool:
    """Whether the journey whose ground-reached departure sits at `at` left home.

    A round trip that opened from home closes there too, so the drive off its
    final landing goes to the house rather than to wherever the itinerary
    happens to put the operator. `engine.build_reconcile_plan` asks this to
    decide which final arrival is a homecoming.

    It used to ask `position_at(at).source == "home"` instead, which is a
    different question wearing the same clothes: "was he at the house at that
    moment" rather than "did this journey start from the house". They agree
    until the operator stages at an airport hotel the night before an early
    flight — then he is at a hotel, but he still left from home, and the proxy
    answers no and routes his drive home to that hotel (#235).

    Asked directly: he opened from home when the planned position IS home, or
    when this departure is the trip's own first transport departure — in which
    case whatever lodging resolves here is a staging stay he drove to from the
    house. A later flight inside a trip already under way is not an opening: a
    round trip flown out of a foreign city during a long stay returns to that
    city's hotel, not across an ocean.

    A lodging check-in only reads as staging when it sits within
    `STAGING_STAY_MAX_LEAD` of that departure. Further back and the operator has
    been living at the destination — he drove there, and a round trip he later
    flies out of is local, returning to the destination rather than the house.

    Returns False when no home address is configured — there is nothing to route
    a homecoming to.
    """
    if home_address is None:
        return False
    planned = resolve_anchor(schedule, at=at, home_address=home_address, home_metros=home_metros)
    if planned.source == "home":
        return planned.address is not None
    if not schedule:
        return False
    trip = _active_trip(schedule, at.astimezone(timezone.utc).date())
    if trip is None:
        return False
    trip_start = _parse_day(trip.get("start"))
    trip_end = _parse_day(trip.get("end"))
    first_departure = _first_transport_departure(schedule, trip_start, trip_end)
    if first_departure is None or at > first_departure:
        return False
    begins_at = _trip_begins_at(schedule, trip_start, trip_end)
    if begins_at is not None and begins_at < first_departure - STAGING_STAY_MAX_LEAD:
        return False
    return True


def resolve_effective_home(home_address: str | None, *, now: datetime) -> str | None:
    """The trip-aware stand-in for the static `home_address` at `now`.

    The I/O convenience over `load_travel_schedule` + `resolve_anchor` for
    callers that treat "home" as a single per-cycle value (flight-assist's
    time-to-leave origin and drive-home destination): off-trip it's the
    static home; on-trip it's the current lodging (or the trip location);
    on-trip with nothing resolvable it's None — the callers' existing
    "no home_address configured" handling then skips routing, which beats
    routing to a residence an ocean away.
    """
    anchor = resolve_anchor(
        load_travel_schedule(), at=now, home_address=home_address, home_metros=home_metro_names()
    )
    return anchor.address
