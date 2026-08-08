"""Lodging-leg source — the getting-there legs of a flight-less trip — pure.

The engine's third leg source, beside the airport chain (`chain.py`) and the
meeting scan (`meeting_source.py`). Those two between them left a whole trip
shape unplanned: lodging booked, no flight, the operator drives. The intra-trip
drives worked (a hotel→event drive is an ordinary meeting leg anchored at the
hotel), but nothing planned the drive that GETS the operator there, because a
hotel check-in is not a flight and so anchors no airport leg. Neither did
anything warn — for a genuine drive trip a missing flight is normal, so the
booking-gap check counted it complete. The trip fell through both nets in
silence (#231).

This module adds the outbound `home → lodging` leg and its symmetric
`lodging → home` return, and decides which flight-less trips get them.

**The origin is home by construction, never `position_at`.** Every other leg
resolves its non-fixed endpoint through the planned-position ladder; this one
must not. At the outbound leg's own anchor the operator is already checked in
by that ladder's reckoning, so `position_at` answers "the hotel" and the leg
collapses to a zero-length drive; anchor earlier and it answers the TRIP's
location (also the destination city), because the pre-departure "still at home"
guard keys on the first FLIGHT and a flight-less trip has none. Getting there
is the one leg whose origin is definitionally home.

**Drive-or-fly is decided by the computed drive time**, in three bands (see
`classify_drive`). The middle band is the interesting one: the engine cannot
tell a long drive from an unbooked flight, so it asks the operator once and
persists the answer (`drive_decision.py`). An answer outranks the band.

Pure: the caller supplies `route`, the home address, the current verdicts, and
the per-trip context derived from the already-planned local drives. Nothing
here reads a clock, a file, or the network — `now` arrives as an argument.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from drive_decision import (  # noqa: E402
    VERDICT_DRIVE,
    VERDICT_FLY,
    VERDICT_UNKNOWN,
    TripVerdict,
)
from lodging import CHECK_IN, CHECK_OUT, hotel_name, lodging_role  # noqa: E402
from reconcile import DesiredBlock  # noqa: E402
from trip_key import trip_key  # noqa: E402
from trip_origin import parse_schedule_time  # noqa: E402

RouteFn = Callable[[str, str], "timedelta | None"]

# The drive-or-fly bands, read off the routed home→lodging drive.
#
# NOT to be merged with `meeting_source.DEFAULT_MAX_REASONABLE_DRIVE`, which is
# also three hours and means the opposite thing: there, a drive that long is
# evidence the operator is somewhere else entirely and the leg is suppressed as
# implausible. Here it is the ceiling under which a drive is unremarkable. Same
# number, opposite conclusion — they drift apart independently.
DRIVE_CERTAIN_MAX = timedelta(hours=3)
DRIVE_IMPLAUSIBLE_MIN = timedelta(hours=7)

# How long a trip's verdict outlives the trip itself, so a verdict recorded for
# a trip still under way is not pruned out from under the return leg.
VERDICT_GRACE = timedelta(days=2)

# Record types that mean the journey to the destination is already booked, so
# it is not a drive. Mirrors `check-travel-bookings.classify_trip`'s
# has_transport (Flight or Rail); Car Rental is deliberately absent — renting
# a car is compatible with driving there, not evidence against it.
_TRANSPORT_TYPES = frozenset({"Flight", "Rail"})

KIND_OUTBOUND = "lodging_outbound"
KIND_RETURN = "lodging_return"


@dataclass(frozen=True)
class DriveTrip:
    """A trip with lodging and no flight — a drive-to-lodging candidate.

    key — `travel-core`'s `trip_key`, the join with the verdict store and the
        booking-gap check.
    address — the lodging's street address, the drive's far endpoint.
    check_in / check_out — the stay's own instants, the fallback anchors.
        `check_out` is None for an orphan check-in, which TripIt does write.
    span_end — the Trip wrapper's own end, the outer bound on "away at the
        destination" when the stay has no check-out to supply one.
    expires — when a verdict about this trip stops applying.
    """

    key: str
    summary: str
    hotel: str
    address: str
    check_in: datetime
    check_out: datetime | None
    span_end: datetime
    expires: datetime


@dataclass(frozen=True)
class TripContext:
    """What a trip's already-planned local drives imply for the outer legs.

    onward_start — when the first drive that STARTS at the lodging leaves it.
        The outbound leg must land by then, not merely by check-in: check-in is
        a nominal mid-afternoon stamp TripIt supplies whether or not anyone
        agreed to it, while the onward drive is anchored on a real commitment.
    trailing_end — when the last local drive finishes, so a return home is not
        planned across an event the operator is still at.
    timezone — the IANA zone those local drives render in, reused so the outer
        legs display in destination-local time too.
    """

    onward_start: datetime | None = None
    trailing_end: datetime | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class TripPlan:
    """One trip's resolved drive-or-fly outcome, for the caller to persist.

    `verdict` is what the band says, `effective` what applies once a recorded
    operator answer is honoured, and `ask` the question owed the operator (None
    when none is). `blocks` is empty for every outcome but a drive.
    """

    trip: DriveTrip
    drive_seconds: int
    verdict: str
    effective: str
    ask: str | None
    blocks: tuple[DesiredBlock, ...]


def _trip_span(record: dict) -> tuple[datetime, datetime] | None:
    start = parse_schedule_time(record.get("start"))
    end = parse_schedule_time(record.get("end"))
    if start is None or end is None or end < start:
        return None
    return start, end


def _in_span(when: datetime, span: tuple[datetime, datetime]) -> bool:
    """Whether a timed item belongs to a trip's date span.

    The Trip wrapper is date-only, so its `end` parses to that day's midnight
    while items on the final day carry real times past it. Compare on dates,
    inclusive of both endpoints — the same reading `trip_origin._active_trip`
    takes.
    """
    start, end = span
    return start.date() <= when.date() <= end.date()


def _has_booked_transport(schedule: list[dict], span: tuple[datetime, datetime]) -> bool:
    """Whether any TIMED transport segment falls inside the trip's span.

    Rail counts alongside Flight: a train to the destination is not a drive
    either, and planning a home→hotel drive around one would double up on a
    journey already booked. `check-travel-bookings` draws the same Flight-or-
    Rail line for its transport gap.

    Date-only records are ignored for the reason
    `trip_origin._first_trip_flight_departure` ignores them: a bare
    `YYYY-MM-DD` cannot say when the operator actually leaves, and treating one
    as booked transport would silently suppress the getting-there legs of a
    trip whose segment the feed never timed.
    """
    for record in schedule:
        if not isinstance(record, dict) or record.get("type") not in _TRANSPORT_TYPES:
            continue
        raw = record.get("start")
        if not (isinstance(raw, str) and "T" in raw):
            continue
        when = parse_schedule_time(raw)
        if when is not None and _in_span(when, span):
            return True
    return False


def _stay_in_span(
    schedule: list[dict], span: tuple[datetime, datetime]
) -> tuple[datetime, datetime | None, str, str] | None:
    """The trip's stay as `(check_in, check_out, hotel, address)`, else None.

    Takes the EARLIEST check-in with a usable address as the trip's arrival
    point and the LATEST check-out as its departure point, so a trip that hops
    between two hotels still yields one outer pair — the engine plans getting
    there and getting home, and the hops between are ordinary local drives.
    """
    check_in: datetime | None = None
    check_out: datetime | None = None
    hotel = ""
    address = ""
    for record in schedule:
        if not isinstance(record, dict) or record.get("type") != "Lodging":
            continue
        when = parse_schedule_time(record.get("start"))
        if when is None or not _in_span(when, span):
            continue
        role = lodging_role(record.get("summary"))
        if role == CHECK_OUT:
            if check_out is None or when > check_out:
                check_out = when
            continue
        if role != CHECK_IN:
            continue
        location = record.get("location")
        if not isinstance(location, str) or not location.strip():
            continue
        if check_in is None or when < check_in:
            check_in = when
            address = location.strip()
            hotel = hotel_name(record.get("summary")) or "lodging"
    if check_in is None:
        return None
    return check_in, check_out, hotel, address


def find_drive_trips(
    schedule: list[dict] | None,
    *,
    now: datetime,
    window: timedelta,
) -> list[DriveTrip]:
    """Every flight-less trip with lodging that starts within `window` of `now`.

    A trip qualifies when it has at least one check-in carrying a usable
    address and no timed flight segment inside its span. Trips already over are
    dropped; a trip under way is kept, since its return leg is still ahead.
    Ordered by check-in.
    """
    if not schedule:
        return []
    horizon = now + window
    trips: list[DriveTrip] = []
    for record in schedule:
        if not isinstance(record, dict) or record.get("type") != "Trip":
            continue
        span = _trip_span(record)
        if span is None:
            continue
        start, end = span
        if end.date() < now.date() or start > horizon:
            continue
        if _has_booked_transport(schedule, span):
            continue
        stay = _stay_in_span(schedule, span)
        if stay is None:
            continue
        check_in, check_out, hotel, address = stay
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        trips.append(
            DriveTrip(
                key=trip_key(summary, start),
                summary=summary.strip(),
                hotel=hotel,
                address=address,
                check_in=check_in,
                check_out=check_out,
                span_end=end,
                expires=(check_out or end) + VERDICT_GRACE,
            )
        )
    trips.sort(key=lambda trip: trip.check_in)
    return trips


def classify_drive(drive: timedelta) -> str:
    """The drive-or-fly verdict the routed home→lodging drive alone supports.

    At or under `DRIVE_CERTAIN_MAX` it is a drive; at or over
    `DRIVE_IMPLAUSIBLE_MIN` it is not; between them the drive time is not
    evidence either way and only the operator can settle it.
    """
    if drive <= DRIVE_CERTAIN_MAX:
        return VERDICT_DRIVE
    if drive >= DRIVE_IMPLAUSIBLE_MIN:
        return VERDICT_FLY
    return VERDICT_UNKNOWN


def _hours(drive: timedelta) -> str:
    """A drive length as a short operator-facing string (`3h40m`, `45m`)."""
    minutes = int(round(drive.total_seconds() / 60))
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def build_question(trip: DriveTrip, drive: timedelta) -> str:
    """The one-time drive-or-fly question for an ambiguous trip.

    Emitted verbatim by the sweep's notice, so it names the trip the way the
    operator's reply will and states the drive that made it ambiguous.
    """
    return (
        f"{trip.summary}: no flight booked, and it's a {_hours(drive)} drive to "
        f"{trip.hotel}. Reply 'drive' and I'll plan the drive, or 'fly' and I'll "
        "flag the missing flight."
    )


def context_from_blocks(
    trips: list[DriveTrip], blocks: list[DesiredBlock]
) -> dict[str, TripContext]:
    """Derive each trip's `TripContext` from the local drives already planned.

    A block counts toward a trip when its anchor falls between that trip's
    check-in and the end of its stay — the window during which the operator is
    at the destination, so any drive anchored in it is a local one this trip's
    outer legs must not overlap.

    An orphan check-in (no check-out record, which TripIt does write) falls back
    to the trip wrapper's own end rather than collapsing the window to the
    check-in instant. Collapsing it made every local drive invisible: the
    outbound ignored the onward drive it must land before, and the return leg
    was dropped as having nothing to depart after even with trailing drives on
    the calendar.
    """
    contexts: dict[str, TripContext] = {}
    for trip in trips:
        window_end = trip.check_out or trip.span_end
        onward: datetime | None = None
        trailing: datetime | None = None
        zone: str | None = None
        for block in blocks:
            if not (trip.check_in <= block.anchor <= window_end):
                continue
            if onward is None or block.start < onward:
                onward = block.start
                zone = block.timezone
            if trailing is None or block.end > trailing:
                trailing = block.end
        contexts[trip.key] = TripContext(onward_start=onward, trailing_end=trailing, timezone=zone)
    return contexts


def _outbound_block(
    trip: DriveTrip, ctx: TripContext, drive: timedelta, home_address: str
) -> DesiredBlock:
    arrive_by = ctx.onward_start or trip.check_in
    return DesiredBlock(
        identity=trip.key,
        kind=KIND_OUTBOUND,
        summary=f"Drive: home → {trip.hotel}",
        start=arrive_by - drive,
        end=arrive_by,
        origin=home_address,
        destination=trip.address,
        baseline_seconds=int(drive.total_seconds()),
        anchor=arrive_by,
        timezone=ctx.timezone,
    )


def _return_block(
    trip: DriveTrip, ctx: TripContext, drive: timedelta, home_address: str
) -> DesiredBlock | None:
    candidates = [when for when in (trip.check_out, ctx.trailing_end) if when is not None]
    if not candidates:
        return None
    depart_after = max(candidates)
    return DesiredBlock(
        identity=trip.key,
        kind=KIND_RETURN,
        summary=f"Drive: {trip.hotel} → home",
        start=depart_after,
        end=depart_after + drive,
        origin=trip.address,
        destination=home_address,
        baseline_seconds=int(drive.total_seconds()),
        anchor=depart_after,
        timezone=ctx.timezone,
    )


def lodging_desired_blocks(
    trips: list[DriveTrip],
    *,
    route: RouteFn,
    home_address: str | None,
    verdicts: dict[str, TripVerdict],
    contexts: dict[str, TripContext] | None = None,
    now: datetime,
) -> tuple[list[DesiredBlock], list[str], list[TripPlan]]:
    """Plan the getting-there legs for each flight-less trip.

    Returns `(blocks, skipped_diagnostics, plans)`. `plans` carries every trip's
    resolved verdict and the question owed on it, for the caller to persist and
    send — this module writes no state and sends nothing.

    A trip yields blocks only when the effective verdict is a drive. A route
    failure or an unconfigured home skips the trip with a diagnostic rather than
    guessing; a leg whose anchor has already passed is dropped the way
    `engine._leg_past` drops a stale airport leg.
    """
    blocks: list[DesiredBlock] = []
    skipped: list[str] = []
    plans: list[TripPlan] = []
    contexts = contexts or {}

    if home_address is None:
        if trips:
            skipped.append(
                f"lodging legs: no home address configured — {len(trips)} drive trip(s) unplanned"
            )
        return blocks, skipped, plans

    for trip in trips:
        outbound_drive = route(home_address, trip.address)
        if outbound_drive is None:
            skipped.append(f"lodging {trip.key}: home→lodging route failed")
            continue

        band = classify_drive(outbound_drive)
        recorded = verdicts.get(trip.key)
        if recorded is not None and recorded.is_operator_answer:
            effective = recorded.verdict
        else:
            effective = band

        ask = None
        if effective == VERDICT_UNKNOWN and (recorded is None or recorded.needs_question):
            ask = build_question(trip, outbound_drive)

        trip_blocks: list[DesiredBlock] = []
        if effective == VERDICT_DRIVE:
            ctx = contexts.get(trip.key, TripContext())
            outbound = _outbound_block(trip, ctx, outbound_drive, home_address)
            if outbound.anchor >= now:
                trip_blocks.append(outbound)
            else:
                skipped.append(f"lodging {trip.key} outbound: past, skipped")

            return_drive = route(trip.address, home_address)
            if return_drive is None:
                skipped.append(f"lodging {trip.key}: lodging→home route failed")
            else:
                returning = _return_block(trip, ctx, return_drive, home_address)
                if returning is None:
                    skipped.append(f"lodging {trip.key} return: no check-out to depart after")
                elif returning.anchor < now:
                    skipped.append(f"lodging {trip.key} return: past, skipped")
                else:
                    trip_blocks.append(returning)
        elif effective == VERDICT_FLY:
            skipped.append(f"lodging {trip.key}: flying — no drive planned")
        else:
            skipped.append(f"lodging {trip.key}: drive-or-fly unresolved — no drive planned")

        blocks.extend(trip_blocks)
        plans.append(
            TripPlan(
                trip=trip,
                drive_seconds=int(outbound_drive.total_seconds()),
                verdict=band,
                effective=effective,
                ask=ask,
                blocks=tuple(trip_blocks),
            )
        )

    return blocks, skipped, plans
