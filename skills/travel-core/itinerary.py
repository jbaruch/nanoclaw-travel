"""The operator's itinerary as one timeline: where he is at any instant (#335).

Every drive leg is anchored at a position: the airport legs at the position
just before a flight or just after it, the meeting legs at the position around
the meeting. This module is the one place that position comes from.

The timeline is built from what moves him and where he sleeps:

- **Transports** (flights and trains) split time into ground periods. Before
  the first transport, between two, after the last: in each, the operator is
  on the ground at the airport the previous transport landed at (the first
  period at the first transport's origin). Transports come from the TripIt
  schedule and from any caller that knows more (the drive engine passes byAir's
  flights): the November trips' flights lived in byAir alone, so a schedule-only
  model never learned the operator had left home (#335). A journey both carry
  keeps the caller's copy, which carries byAir's live times.
- **Lodging stays** (check-in to check-out, paired by hotel) are nominal: TripIt
  stamps a standard check-in hour whether or not he can be there yet. Each stay
  belongs to the ground period it overlaps most, and counts only inside it. A
  hotel in the trip's city reached by a 17:00 flight starts when that flight
  lands, however early its check-in stamp; an airport hotel slept in before an
  early flight belongs to the period before that flight.

Position at an instant, in its ground period:

1. a stay covering it: that lodging;
2. home, when the period began with a landing at a home airport; or, for the
   first period (before any transport) and whenever home airports are unknown,
   when no trip covers the day or the trip covering it has not begun (its first
   transport or stay is still ahead, or it has none booked);
3. otherwise away: the lodging he last checked out of on this trip (between the
   legs of a journey, or between a check-out and the next check-in), else the
   trip's location, else unresolved (no address).

`home_airports` are the profile's `home_airport` codes. Without them, a begun
trip reads as away until its span ends.

Pure and stdlib-only: no clock, no file, no network. `trip_origin` wraps it for
callers holding a schedule.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from lodging import CHECK_IN, CHECK_OUT, hotel_name, lodging_role  # noqa: E402

# A flight segment's route in its summary or description, e.g. "WN716 BNA to SAT".
ROUTE_RE = re.compile(r"\b([A-Z]{3})\s+to\s+([A-Z]{3})\b")

# Record types that move the operator between places.
_TRANSPORT_TYPES = frozenset({"Flight", "Rail"})

# Two records of one transport (the feed repeats a flight, byAir and TripIt both
# carry it) depart within this of each other between the same places.
_SAME_TRANSPORT = timedelta(hours=6)

# A stay with a check-in but no check-out, or the reverse, is bounded by the
# trip covering its one known end, else by this much from that end.
_OPEN_STAY = timedelta(days=1)

# A check-in with no check-out belongs to the ground period it falls in when
# at least this long remains before that period's departure: a night slept
# there, an airport hotel before an early flight. Less, and the departure is
# the flight to it: its nominal check-in came before he could arrive.
_SLEPT_BEFORE_DEPARTURE = timedelta(hours=6)

HOME = "home"
LODGING = "lodging"
TRIP_LOCATION = "trip_location"
UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Transport:
    """One flight or train: UTC `departs` / `arrives`, IATA `origin` /
    `destination` when known (a train carries none)."""

    departs: datetime
    arrives: datetime
    origin: str | None = None
    destination: str | None = None


@dataclass(frozen=True)
class Position:
    """Where the operator is: `address` (None when unresolved), the rule that
    placed him (`source`: home / lodging / trip_location / unresolved), and a
    human `detail` for diagnostics."""

    address: str | None
    source: str
    detail: str | None = None


@dataclass(frozen=True)
class _Stay:
    start: datetime
    end: datetime
    address: str | None  # None: booked, but TripIt gave no address
    name: str
    open_end: bool = False  # no check-out record
    open_start: bool = False  # no check-in record (a stay in progress)


@dataclass(frozen=True)
class _Period:
    start: datetime | None  # None: open to the past
    end: datetime | None  # None: open to the future
    airport: str | None
    stays: tuple[_Stay, ...] = ()

    def contains(self, at: datetime) -> bool:
        return (self.start is None or self.start <= at) and (self.end is None or at < self.end)


@dataclass(frozen=True)
class _Trip:
    start: date
    end: date
    location: str | None
    summary: str


def parse_time(value: object) -> datetime | None:
    """A schedule `start` / `end` as a UTC instant, else None. A date-only value
    reads as that day's midnight UTC; a naive one as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timed(value: object) -> datetime | None:
    """A TIMED schedule value as an instant: a date-only one carries no time of
    day, and read as midnight it would place a departure hours early."""
    if not (isinstance(value, str) and "T" in value):
        return None
    return parse_time(value)


def flight_route(record: dict) -> tuple[str, str] | None:
    """`(origin, destination)` IATA codes from a flight record's text, else None."""
    for field in ("description", "summary"):
        text = record.get(field)
        if isinstance(text, str):
            match = ROUTE_RE.search(text)
            if match:
                return match.group(1), match.group(2)
    return None


def schedule_transports(schedule: list[dict] | None) -> list[Transport]:
    """The timed flights and trains in a TripIt schedule."""
    out: list[Transport] = []
    for record in schedule or []:
        if not isinstance(record, dict) or record.get("type") not in _TRANSPORT_TYPES:
            continue
        departs, arrives = _timed(record.get("start")), _timed(record.get("end"))
        if departs is None or arrives is None or arrives <= departs:
            continue
        route = flight_route(record) if record.get("type") == "Flight" else None
        out.append(
            Transport(
                departs=departs,
                arrives=arrives,
                origin=route[0] if route else None,
                destination=route[1] if route else None,
            )
        )
    return out


def _dedupe(preferred: list[Transport], rest: list[Transport]) -> list[Transport]:
    """One transport per physical journey, ordered by departure. A journey
    both lists carry keeps its `preferred` copy: the caller's byAir flights
    carry live times, the schedule's copy only the booked ones."""
    kept: list[Transport] = []
    for transport in sorted(preferred, key=_by_departure) + sorted(rest, key=_by_departure):
        if any(
            other.origin == transport.origin
            and other.destination == transport.destination
            and abs(other.departs - transport.departs) <= _SAME_TRANSPORT
            for other in kept
        ):
            continue
        kept.append(transport)
    return sorted(kept, key=_by_departure)


def _by_departure(transport: Transport) -> tuple[datetime, datetime]:
    return (transport.departs, transport.arrives)


def _trips(schedule: list[dict]) -> list[_Trip]:
    trips = []
    for record in schedule:
        if not isinstance(record, dict) or record.get("type") != "Trip":
            continue
        start, end = parse_time(record.get("start")), parse_time(record.get("end"))
        if start is None or end is None or end < start:
            continue
        location = record.get("location")
        trips.append(
            _Trip(
                start=start.date(),
                end=end.date(),
                location=location.strip()
                if isinstance(location, str) and location.strip()
                else None,
                summary=str(record.get("summary") or "trip"),
            )
        )
    return trips


def _trip_on(trips: list[_Trip], day: date) -> _Trip | None:
    """The trip whose date span covers `day`, the latest-starting one when several
    do. The span is inclusive at both ends."""
    covering = [trip for trip in trips if trip.start <= day <= trip.end]
    return max(covering, key=lambda trip: trip.start) if covering else None


def _stays(schedule: list[dict], trips: list[_Trip]) -> list[_Stay]:
    """Lodging stays: each check-in paired with the next check-out of the same
    hotel, or one record spanning the stay when it carries neither prefix. A
    check-in without a check-out ends with its trip, a check-out without a
    check-in starts with its trip (a stay in progress, whose check-in the feed
    has dropped); outside any trip, `_OPEN_STAY` from the known end. A stay with
    no address still marks where the operator sleeps, with no address to drive
    to."""
    events: list[tuple[datetime, str, str, str | None]] = []
    stays: list[_Stay] = []
    for record in schedule:
        if not isinstance(record, dict) or record.get("type") != "Lodging":
            continue
        location = record.get("location")
        address = location.strip() if isinstance(location, str) and location.strip() else None
        when, role = parse_time(record.get("start")), lodging_role(record.get("summary"))
        if role is None:
            # No check-in / check-out prefix: one record spanning the whole stay,
            # or, with no end after its start, a check-in.
            start, end = _timed(record.get("start")), _timed(record.get("end"))
            name = str(record.get("summary") or "lodging")
            if start is not None and end is not None and end > start:
                stays.append(_Stay(start=start, end=end, address=address, name=name))
            elif when is not None:
                events.append((when, CHECK_IN, name, address))
            continue
        name = hotel_name(record.get("summary"))
        if when is None or name is None:
            continue
        events.append((when, role, name, address))
    events.sort(key=lambda event: event[0])

    used: set[int] = set()
    for index, (when, role, name, address) in enumerate(events):
        if role != CHECK_IN or index in used:
            continue
        out_index = next(
            (
                j
                for j in range(index + 1, len(events))
                if j not in used and events[j][1] == CHECK_OUT and events[j][2] == name
            ),
            None,
        )
        open_end = out_index is None
        if out_index is not None:
            used.add(out_index)
            end = events[out_index][0]
            address = address or events[out_index][3]
        else:
            trip = _trip_on(trips, when.date())
            end = (
                datetime.combine(trip.end + timedelta(days=1), datetime.min.time(), UTC)
                if trip is not None
                else when + _OPEN_STAY
            )
        used.add(index)
        if end > when:
            stays.append(_Stay(start=when, end=end, address=address, name=name, open_end=open_end))
    for index, (when, role, name, address) in enumerate(events):
        if role != CHECK_OUT or index in used:
            continue
        trip = _trip_on(trips, when.date())
        start = (
            datetime.combine(trip.start, datetime.min.time(), UTC)
            if trip is not None
            else when - _OPEN_STAY
        )
        if start < when:
            stays.append(_Stay(start=start, end=when, address=address, name=name, open_start=True))
    return stays


def _overlap(stay: _Stay, period: _Period) -> timedelta:
    start = stay.start if period.start is None else max(stay.start, period.start)
    end = stay.end if period.end is None else min(stay.end, period.end)
    return max(end - start, timedelta(0))


def _home_period(stay: _Stay, periods: list[_Period]) -> int | None:
    """The index of the ground period a stay belongs to, else None.

    A stay with both ends belongs where it overlaps most. One with only a
    check-out belongs to the period it is checked out of. One with only a
    check-in belongs to the period it starts in, or to the next period when that
    one's departure comes within `_SLEPT_BEFORE_DEPARTURE` of the check-in."""
    if stay.open_start:
        return next(
            (i for i, p in enumerate(periods) if p.contains(stay.end - timedelta(seconds=1))), None
        )
    if stay.open_end:
        index = next((i for i, p in enumerate(periods) if p.contains(stay.start)), None)
        if index is None:
            return None
        period = periods[index]
        if (
            period.end is not None
            and period.end - stay.start < _SLEPT_BEFORE_DEPARTURE
            and index + 1 < len(periods)
        ):
            return index + 1
        return index
    overlaps = [_overlap(stay, period) for period in periods]
    best = max(range(len(periods)), key=lambda i: overlaps[i])
    return best if overlaps[best] > timedelta(0) else None


def _periods(transports: list[Transport], stays: list[_Stay]) -> list[_Period]:
    """The ground periods between transports, each holding the stays that
    overlap it more than any other period, cut to the period."""
    bounds: list[tuple[datetime | None, datetime | None, str | None]] = []
    previous: Transport | None = None
    for transport in transports:
        start = previous.arrives if previous is not None else None
        airport = previous.destination if previous is not None else transport.origin
        if start is None or start < transport.departs:
            bounds.append((start, transport.departs, airport))
        previous = transport
    bounds.append(
        (
            previous.arrives if previous is not None else None,
            None,
            previous.destination if previous is not None else None,
        )
    )
    periods = [_Period(start=s, end=e, airport=a) for s, e, a in bounds]

    assigned: dict[int, list[_Stay]] = {}
    for stay in stays:
        best = _home_period(stay, periods)
        if best is None:
            continue
        period = periods[best]
        cut = _Stay(
            start=stay.start if period.start is None else max(stay.start, period.start),
            end=stay.end if period.end is None else min(stay.end, period.end),
            address=stay.address,
            name=stay.name,
        )
        if cut.end <= cut.start:
            continue
        assigned.setdefault(best, []).append(cut)
    return [
        _Period(
            start=p.start,
            end=p.end,
            airport=p.airport,
            stays=tuple(sorted(assigned.get(i, []), key=lambda s: s.start)),
        )
        for i, p in enumerate(periods)
    ]


class Itinerary:
    """The operator's timeline. Build once per run; ask `position_at` per leg.

    `schedule` is the TripIt record list (None for none), `transports` any
    flights or trains the caller knows beyond it (preferred over the
    schedule's copy of the same journey), `home_address` the static
    residence (None when unconfigured: a home position then has no address),
    `home_airports` the IATA codes of the airports home is reached by.
    """

    def __init__(
        self,
        schedule: list[dict] | None,
        *,
        home_address: str | None,
        home_airports: frozenset[str] = frozenset(),
        transports: list[Transport] | tuple[Transport, ...] = (),
    ):
        records = [record for record in schedule or [] if isinstance(record, dict)]
        self._home = home_address
        self._home_airports = frozenset(code.strip().upper() for code in home_airports if code)
        self._trips = _trips(records)
        self._transports = _dedupe(list(transports), schedule_transports(records))
        self._periods = _periods(self._transports, _stays(records, self._trips))

    def _period_index(self, at: datetime) -> int:
        for index, period in enumerate(self._periods):
            if period.contains(at):
                return index
        # In the air: the period he is travelling to.
        return next(
            (
                index
                for index, period in enumerate(self._periods)
                if period.start is not None and period.start > at
            ),
            len(self._periods) - 1,
        )

    def _home_position(self, detail: str | None = None) -> Position:
        return Position(address=self._home, source=HOME, detail=detail)

    def _trip_begun(self, trip: _Trip, at: datetime) -> bool:
        """Whether, by `at`, the operator has left for `trip`: its first transport
        departed or its first stay began. A trip with neither booked has not
        begun: nothing says he left (a local placeholder trip, #314)."""
        starts = [
            t.departs for t in self._transports if trip.start <= t.departs.date() <= trip.end
        ] + [
            stay.start
            for period in self._periods
            for stay in period.stays
            if trip.start <= stay.start.date() <= trip.end
        ]
        return bool(starts) and at >= min(starts)

    def _is_home(self, period: _Period, at: datetime) -> bool:
        """Whether the operator is home at `at`, absent a lodging stay.

        A period that begins with a landing is judged by where he landed: a home
        airport is home, any other is away, whatever the trip wrappers say (a
        flight the evening before a trip's first day still took him away). The
        first period, before any transport, reaches back into the past and says
        nothing by its airport, and neither does a period when home airports are
        unknown: there, home with no trip covering the day or while that trip
        has not begun, away once it has."""
        if self._home_airports and period.airport is not None and period.start is not None:
            return period.airport in self._home_airports
        trip = _trip_on(self._trips, at.date())
        return trip is None or not self._trip_begun(trip, at)

    def position_at(self, at: datetime) -> Position:
        """Where the operator is at `at` (tz-aware); see the module docstring."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Itinerary.position_at: `at` must be timezone-aware (UTC)")
        at = at.astimezone(UTC)
        index = self._period_index(at)
        period = self._periods[index]
        covering = [stay for stay in period.stays if stay.start <= at < stay.end]
        if covering:
            stay = max(covering, key=lambda s: s.start)
            if stay.address is not None:
                return Position(address=stay.address, source=LODGING, detail=stay.name)
            return self._away_fallback(period, at, f"at {stay.name}, which has no address")
        if self._is_home(period, at):
            return self._home_position()
        left = self._last_checked_out(index, at)
        if left is not None:
            return Position(address=left.address, source=LODGING, detail=f"after {left.name}")
        return self._away_fallback(period, at, None)

    def _last_checked_out(self, index: int, at: datetime) -> _Stay | None:
        """The lodging last checked out of before `at`, in this away period or the
        away periods before it: between two legs of one journey, and after a
        check-out before the next check-in, that hotel is still the anchor. Only
        a stay of the trip covering `at` counts, so an earlier trip's hotel never
        anchors this one; with no trip, only this period's. The search stops at a
        period spent at home."""
        trip = _trip_on(self._trips, at.date())
        for i in range(index, -1, -1):
            period = self._periods[i]
            if i < index and (trip is None or self._is_home(period, period.end or at)):
                return None
            left = [
                s
                for s in period.stays
                if s.end <= at
                and s.address is not None
                and (trip is None or s.end.date() >= trip.start)
            ]
            if left:
                return max(left, key=lambda s: s.end)
        return None

    def _away_fallback(self, period: _Period, at: datetime, why: str | None) -> Position:
        """Away with no lodging address: the trip's location, else unresolved."""
        trip = _trip_on(self._trips, at.date())
        if trip is not None and trip.location is not None:
            return Position(address=trip.location, source=TRIP_LOCATION, detail=why or trip.summary)
        where = trip.summary if trip is not None else f"near {period.airport or 'an unknown place'}"
        return Position(
            address=None,
            source=UNRESOLVED,
            detail=f"away ({where}) with no lodging or trip location at {at.isoformat()}",
        )
