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

**The outer legs skip the lodging when the operator would not stop there.**
Anchoring the outbound to land as the first local drive departs the hotel is
zero dwell — an arrival and a departure at the same instant, a stop made only
to leave again. When that first local drive LEAVES the lodging the outbound
goes straight to its venue and absorbs it; the operator reaches the hotel on
that event's own return leg, checking in when they actually do. The return
mirrors it: a last local drive back to a lodging already checked out of is
absorbed, and the drive home departs the venue. Both absorptions are reported
on `TripPlan.subsumed` for the caller to drop — planning both halves would put
two drives on the calendar for one journey. A failed direct route degrades to
the via-the-lodging shape rather than dropping the leg.

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
from reconcile import DesiredBlock, HeldLeg  # noqa: E402
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

# How far from the lodging an event can be and still be part of THAT trip.
#
# A third constant at three hours, and a third meaning — kept apart from the
# two above for the reason their own comment gives. `DRIVE_CERTAIN_MAX` is "a
# drive short enough that the operator is certainly driving to this trip";
# `meeting_source.DEFAULT_MAX_REASONABLE_DRIVE` is "a drive so long the
# operator cannot be positioned to make it"; this is "an event near enough the
# hotel to be part of this stay". Generous on purpose: excluding a genuine trip
# event costs a suppressed drive, and the alternative reading (every meeting on
# these dates belongs to the trip) is what #243 got wrong.
LOCAL_TO_LODGING_MAX = timedelta(hours=3)

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
    span_start / span_end — the Trip wrapper's own dates, the bounds on "away
        at the destination". Independent of where the stay's stamps sit, so the
        plan does not move when the operator restamps a check-in (#242).
    expires — when a verdict about this trip stops applying.
    """

    key: str
    summary: str
    hotel: str
    address: str
    check_in: datetime
    check_out: datetime | None
    span_start: datetime
    span_end: datetime
    expires: datetime


@dataclass(frozen=True)
class LocalLeg:
    """One local drive between the lodging and a venue, as the outer legs see it.

    `venue` is the endpoint that is NOT the lodging, `label` the event name the
    drive was planned for. `identity` / `kind` key the block so a caller can
    drop it when an outer leg subsumes it.
    """

    identity: str
    kind: str
    label: str
    venue: str
    start: datetime
    end: datetime

    @property
    def key(self) -> tuple[str, str]:
        return (self.identity, self.kind)


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
    first_out — the first local drive that LEAVES the lodging, when there is
        one. The outbound goes straight to its venue instead of through the
        hotel: landing at the lodging exactly as that drive departs it is zero
        dwell, a stop the operator makes only to leave it again.
    last_back — the last local drive that RETURNS to the lodging. The return
        home departs from its venue instead when the operator has already
        checked out by then, for the mirror reason.
    """

    onward_start: datetime | None = None
    trailing_end: datetime | None = None
    timezone: str | None = None
    first_out: LocalLeg | None = None
    last_back: LocalLeg | None = None


@dataclass(frozen=True)
class TripPlan:
    """One trip's resolved drive-or-fly outcome, for the caller to persist.

    `verdict` is what the band says, `effective` what applies once a recorded
    operator answer is honoured, and `ask` the question owed the operator (None
    when none is). `blocks` is empty for every outcome but a drive.

    `subsumed` carries the `(identity, kind)` of every local drive an outer leg
    absorbed — a hotel→venue drive the operator never makes because the
    outbound went straight to the venue. The caller drops those blocks before
    reconciling; leaving them in would plan both halves of a journey made once.
    """

    trip: DriveTrip
    drive_seconds: int
    verdict: str
    effective: str
    ask: str | None
    blocks: tuple[DesiredBlock, ...]
    subsumed: tuple[tuple[str, str], ...] = ()


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

    Date-only records are ignored for the reason `trip_origin._timed_within`
    ignores them: a bare `YYYY-MM-DD` cannot say when the operator actually
    leaves, and treating one as booked transport would silently suppress the
    getting-there legs of a trip whose segment the feed never timed.
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
                span_start=start,
                span_end=end,
                expires=(check_out or end) + VERDICT_GRACE,
            )
        )
    trips.sort(key=lambda trip: trip.check_in)
    return trips


def effective_verdict(trip: DriveTrip, drive: timedelta, verdicts: dict[str, TripVerdict]) -> str:
    """The verdict in force for a trip — the operator's answer, else the band."""
    recorded = verdicts.get(trip.key)
    if recorded is not None and recorded.is_operator_answer:
        return recorded.verdict
    return classify_drive(drive)


def driving_trips(
    trips: list[DriveTrip],
    *,
    route: RouteFn,
    home_address: str | None,
    verdicts: dict[str, TripVerdict],
) -> list[DriveTrip]:
    """The trips the operator is known to DRIVE to.

    The caller needs this before planning the meeting side, to spare those
    trips' own events the away-suppression (`meeting_source`, #242). A trip
    whose home→lodging route fails is not claimed as a drive — the same
    conservative reading `lodging_desired_blocks` takes when it skips one.
    """
    if home_address is None:
        return []
    driving: list[DriveTrip] = []
    for trip in trips:
        drive = route(home_address, trip.address)
        if drive is None:
            continue
        if effective_verdict(trip, drive, verdicts) == VERDICT_DRIVE:
            driving.append(trip)
    return driving


def meetings_on_trip(trip: DriveTrip, meetings: list, *, route: RouteFn) -> dict[str, timedelta]:
    """`meetings` belonging to `trip`, as id → drive from its lodging.

    Membership is dates AND reachability. The drive itself is returned, not
    just the id, so a caller holding overlapping trips can settle which one a
    meeting belongs to instead of letting iteration order decide (#245).

    Dates alone are not evidence of belonging. Every meeting occurring while a
    trip is under way would qualify, including a home appointment the operator
    never cancelled and a meeting belonging to an overlapping trip. Those then
    bypass the implausible-drive suppression and have their endpoints rewritten
    to this trip's lodging — reintroducing the invented cross-country drive
    that suppression exists to prevent.

    So a meeting belongs when it is also REACHABLE from the trip's lodging: a
    local drive, under `LOCAL_TO_LODGING_MAX`. The venue is where the operator
    would actually go from that hotel, which is the question `TripPresence`
    needs answered. A meeting with no parsed start, no id, no location, or an
    unroutable one belongs to no trip — the conservative reading, since the
    only thing membership grants is an exemption.

    Date comparison for the reason `_in_span` gives: the Trip wrapper is
    date-only, so an instant comparison drops the final day's evening events.
    The upper bound matches `context_from_blocks` — a stay recorded past the
    wrapper's end extends it.
    """
    last_day = max(trip.span_end, trip.check_out) if trip.check_out else trip.span_end
    reachable: dict[str, timedelta] = {}
    for meeting in meetings:
        start = getattr(meeting, "start", None)
        meeting_id = getattr(meeting, "meeting_id", None)
        location = getattr(meeting, "location", None)
        if start is None or not meeting_id or not location:
            continue
        if not (trip.span_start.date() <= start.date() <= last_day.date()):
            continue
        local = route(trip.address, location)
        if local is not None and local <= LOCAL_TO_LODGING_MAX:
            reachable[meeting_id] = local
    return reachable


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


def _local_leg(block: DesiredBlock, *, venue: str) -> LocalLeg:
    """A planned local drive as the outer legs need to see it."""
    label = block.summary.removeprefix("Drive: ").strip() or venue
    return LocalLeg(
        identity=block.identity,
        kind=block.kind,
        label=label,
        venue=venue,
        start=block.start,
        end=block.end,
    )


def context_from_blocks(
    trips: list[DriveTrip], blocks: list[DesiredBlock]
) -> dict[str, TripContext]:
    """Derive each trip's `TripContext` from the local drives already planned.

    A block counts toward a trip when its anchor falls between that trip's
    check-in and the end of the trip — the window during which the operator is
    at the destination, so any drive anchored in it is a local one this trip's
    outer legs must not overlap.

    The window closes at the end of the trip's last DAY, never at check-out.
    Check-out releases the room, not the operator: an evening event on the
    check-out day is the ordinary shape of a weekend trip, and clamping at
    check-out hid every drive after it. The return leg then departed at
    check-out and landed home hours before a game the calendar still had the
    operator driving to from a hotel they had left.

    The upper bound compares DATES, the reading `_in_span` takes for the same
    wrapper: date-only `end` parses to that day's midnight while items on the
    final day carry real times past it. An instant comparison drops the last
    evening's drives — the ones the return leg most needs to depart after.

    An orphan check-in (no check-out record, which TripIt does write) needs no
    special case under that bound. Collapsing the window to the check-in instant
    made every local drive invisible: the outbound ignored the onward drive it
    must land before, and the return leg was dropped as having nothing to depart
    after even with trailing drives on the calendar.

    Both bounds are the trip's, never the stay's. Keying the lower bound on
    check-in made the plan depend on where the operator stamped it: a check-in
    after the trip's first event hid that event's drives, and the outbound
    re-anchored on the next day's (#242). A check-in time is a fact about a
    reservation, not an instruction about which drives count.

    The trip's VENUES are the endpoints its lodging-touching drives reach, and
    `first_out` is the first drive INTO one of them — from the hotel or from
    home, whichever the position ladder produced. Keying on "leaves the
    lodging" assumed the operator checks in before doing anything, which is the
    same check-in dependence by another name. Deriving venues from the
    lodging-touching drives is what keeps a home→dentist errand on the trip's
    first morning from reading as the trip's first commitment.
    """
    contexts: dict[str, TripContext] = {}
    for trip in trips:
        # `max` rather than `span_end` alone: a stay TripIt records past the
        # wrapper's end still bounds the window at the later of the two.
        last_day = max(trip.span_end, trip.check_out) if trip.check_out else trip.span_end
        in_window = [
            block
            for block in blocks
            if trip.span_start.date() <= block.anchor.date() <= last_day.date()
        ]
        venues = {
            block.destination if block.origin == trip.address else block.origin
            for block in in_window
            if trip.address in (block.origin, block.destination)
        } - {trip.address}

        onward: datetime | None = None
        trailing: datetime | None = None
        zone: str | None = None
        first_out: LocalLeg | None = None
        last_back: LocalLeg | None = None
        for block in in_window:
            if not venues & {block.origin, block.destination}:
                continue
            if onward is None or block.start < onward:
                onward = block.start
                zone = block.timezone
            if trailing is None or block.end > trailing:
                trailing = block.end
            if block.destination in venues and (first_out is None or block.start < first_out.start):
                first_out = _local_leg(block, venue=block.destination)
            if block.destination == trip.address and (
                last_back is None or block.end > last_back.end
            ):
                last_back = _local_leg(block, venue=block.origin)
        contexts[trip.key] = TripContext(
            onward_start=onward,
            trailing_end=trailing,
            timezone=zone,
            first_out=first_out,
            last_back=last_back,
        )
    return contexts


def _released_return(trip: DriveTrip, ctx: TripContext) -> LocalLeg | None:
    """The trip's last local drive when it comes back to a lodging already
    checked out of: the return home then leaves from its venue."""
    back = ctx.last_back
    if back is not None and trip.check_out is not None and back.start >= trip.check_out:
        return back
    return None


def _outbound_anchor(trip: DriveTrip, ctx: TripContext) -> datetime:
    """When the outbound must land: the first local drive's departure, else
    check-in — `_outbound_block`'s default."""
    return ctx.onward_start or trip.check_in


def _return_anchor(trip: DriveTrip, ctx: TripContext) -> datetime | None:
    """When the return leaves: from the venue of a released-room last drive,
    else the later of check-out and the last local drive — `_return_block`'s
    default. None when the trip has neither."""
    released = _released_return(trip, ctx)
    if released is not None:
        return released.start
    candidates = [when for when in (trip.check_out, ctx.trailing_end) if when is not None]
    return max(candidates) if candidates else None


def _outbound_block(
    trip: DriveTrip,
    ctx: TripContext,
    drive: timedelta,
    home_address: str,
    *,
    destination: str | None = None,
    label: str | None = None,
    arrive_by: datetime | None = None,
) -> DesiredBlock:
    """The leg that gets the operator to the trip.

    Defaults to home→lodging landing by the first local drive. The caller
    overrides the far end when that first drive leaves the lodging, so the
    outbound goes straight to the venue rather than through a hotel the
    operator would arrive at and depart from in the same instant.
    """
    arrive_by = arrive_by or _outbound_anchor(trip, ctx)
    return DesiredBlock(
        identity=trip.key,
        kind=KIND_OUTBOUND,
        summary=f"Drive: home → {label or trip.hotel}",
        start=arrive_by - drive,
        end=arrive_by,
        origin=home_address,
        destination=destination or trip.address,
        baseline_seconds=int(drive.total_seconds()),
        anchor=arrive_by,
        timezone=ctx.timezone,
    )


def _return_block(
    trip: DriveTrip,
    ctx: TripContext,
    drive: timedelta,
    home_address: str,
    *,
    origin: str | None = None,
    label: str | None = None,
    depart_after: datetime | None = None,
) -> DesiredBlock | None:
    """The leg that gets the operator home.

    Defaults to lodging→home departing after the later of check-out and the
    last local drive. The caller overrides the near end when that last drive
    returns to a lodging already checked out of, so the drive home starts at
    the venue rather than doubling back to a room the operator has released.
    """
    if depart_after is None:
        candidates = [when for when in (trip.check_out, ctx.trailing_end) if when is not None]
        if not candidates:
            return None
        depart_after = max(candidates)
    return DesiredBlock(
        identity=trip.key,
        kind=KIND_RETURN,
        summary=f"Drive: {label or trip.hotel} → home",
        start=depart_after,
        end=depart_after + drive,
        origin=origin or trip.address,
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
) -> tuple[list[DesiredBlock], list[str], list[TripPlan], list[HeldLeg]]:
    """Plan the getting-there legs for each flight-less trip.

    Returns `(blocks, skipped_diagnostics, plans, held)`. `plans` carries every
    trip's resolved verdict and the question owed on it, for the caller to
    persist and send — this module writes no state and sends nothing.

    A trip yields blocks only when the effective verdict is a drive. A route
    failure or an unconfigured home skips the trip with a diagnostic rather than
    guessing, and so does a leg whose anchor has already passed, the way
    `engine._leg_past` treats a stale airport leg. Each of those legs is HELD
    (#334): its current block is kept rather than orphan-deleted. A trip the
    verdict says is flown, or not yet decided, plans no leg and holds none.
    """
    blocks: list[DesiredBlock] = []
    skipped: list[str] = []
    plans: list[TripPlan] = []
    held: list[HeldLeg] = []
    contexts = contexts or {}

    def hold(trip: DriveTrip, kind: str, when: datetime | None, reason: str) -> None:
        skipped.append(reason)
        summary = (
            f"Drive: home → {trip.hotel}"
            if kind == KIND_OUTBOUND
            else f"Drive: {trip.hotel} → home"
        )
        held.append(
            HeldLeg(identity=trip.key, kind=kind, summary=summary, when=when, reason=reason)
        )

    def hold_absorbed(trip: DriveTrip, local: LocalLeg, reason: str) -> None:
        """Hold a local drive an earlier sweep may have absorbed into the trip's
        outer leg: recreating it beside that retained block would put a second
        drive on the calendar for one journey. The caller drops a held local leg
        from the desired set; the outer leg, held with it, is the one reported."""
        held.append(
            HeldLeg(
                identity=local.identity,
                kind=local.kind,
                summary=f"Drive: {local.label}",
                when=local.start,
                reason=f"{reason} — its outer legs may absorb this drive",
                announce=False,
            )
        )

    def hold_trip(trip: DriveTrip, reason: str) -> None:
        """Hold both legs of a trip that could not be planned at all, unless the
        operator said it is flown: a flown trip has no drive to keep. The trip's
        first and last local drives are held with them (`hold_absorbed`)."""
        recorded = verdicts.get(trip.key)
        if recorded is not None and recorded.is_operator_answer and recorded.verdict == VERDICT_FLY:
            skipped.append(f"{reason} — flying, nothing held")
            return
        ctx = contexts.get(trip.key, TripContext())
        # Only a local drive an outer leg can absorb: the first one out, and
        # the last one back only once the room is released.
        for local in (ctx.first_out, _released_return(trip, ctx)):
            if local is not None:
                hold_absorbed(trip, local, reason)
        skipped.append(reason)
        held.append(
            HeldLeg(
                identity=trip.key,
                kind=KIND_OUTBOUND,
                summary=f"Drive: home → {trip.hotel}",
                when=_outbound_anchor(trip, ctx),
                reason=reason,
            )
        )
        held.append(
            HeldLeg(
                identity=trip.key,
                kind=KIND_RETURN,
                summary=f"Drive: {trip.hotel} → home",
                when=_return_anchor(trip, ctx),
                reason=reason,
            )
        )

    if home_address is None:
        for trip in trips:
            hold_trip(trip, f"lodging {trip.key}: no home address configured — unplanned")
        return blocks, skipped, plans, held

    for trip in trips:
        outbound_drive = route(home_address, trip.address)
        if outbound_drive is None:
            hold_trip(trip, f"lodging {trip.key}: home→lodging route failed")
            continue

        band = classify_drive(outbound_drive)
        effective = effective_verdict(trip, outbound_drive, verdicts)
        recorded = verdicts.get(trip.key)

        ask = None
        if effective == VERDICT_UNKNOWN and (recorded is None or recorded.needs_question):
            ask = build_question(trip, outbound_drive)

        trip_blocks: list[DesiredBlock] = []
        subsumed: list[tuple[str, str]] = []
        if effective == VERDICT_DRIVE:
            ctx = contexts.get(trip.key, TripContext())

            # Outbound. When the trip's first local drive LEAVES the lodging,
            # going through the hotel means arriving exactly as that drive
            # departs — a stop made only to leave it. Drive to the venue
            # instead and absorb that local leg. A route failure to the venue
            # falls back to the hotel rather than dropping the leg.
            out = ctx.first_out
            direct_out = route(home_address, out.venue) if out is not None else None
            if out is not None and direct_out is None:
                skipped.append(
                    f"lodging {trip.key} outbound: home→{out.label} route failed — "
                    "routing via the lodging"
                )
            if out is not None and direct_out is not None:
                outbound = _outbound_block(
                    trip,
                    ctx,
                    direct_out,
                    home_address,
                    destination=out.venue,
                    label=out.label,
                    arrive_by=out.end,
                )
                absorbs = out.key
            else:
                outbound = _outbound_block(trip, ctx, outbound_drive, home_address)
                absorbs = None
            if outbound.anchor >= now:
                trip_blocks.append(outbound)
                if absorbs is not None:
                    subsumed.append(absorbs)
            else:
                # The outbound is not planned, so a local drive it would have
                # absorbed has to stand on its own.
                hold(
                    trip,
                    KIND_OUTBOUND,
                    outbound.anchor,
                    f"lodging {trip.key} outbound: past, skipped",
                )

            # Return. Mirror case: the last local drive comes BACK to a lodging
            # the operator has already checked out of, so leave from the venue.
            released = _released_return(trip, ctx)
            returning: DesiredBlock | None = None
            absorbs_back: tuple[str, str] | None = None
            if released is not None:
                direct_home = route(released.venue, home_address)
                if direct_home is None:
                    skipped.append(
                        f"lodging {trip.key} return: {released.label}→home route failed — "
                        "routing via the lodging"
                    )
                else:
                    returning = _return_block(
                        trip,
                        ctx,
                        direct_home,
                        home_address,
                        origin=released.venue,
                        label=released.label,
                        depart_after=released.start,
                    )
                    absorbs_back = released.key
            if returning is None:
                return_drive = route(trip.address, home_address)
                if return_drive is None:
                    reason = f"lodging {trip.key}: lodging→home route failed"
                    hold(trip, KIND_RETURN, _return_anchor(trip, ctx), reason)
                    if released is not None:
                        hold_absorbed(trip, released, reason)
                else:
                    returning = _return_block(trip, ctx, return_drive, home_address)
                    if returning is None:
                        hold(
                            trip,
                            KIND_RETURN,
                            trip.check_out,
                            f"lodging {trip.key} return: no check-out to depart after",
                        )
            if returning is not None:
                if returning.anchor < now:
                    hold(
                        trip,
                        KIND_RETURN,
                        returning.anchor,
                        f"lodging {trip.key} return: past, skipped",
                    )
                else:
                    trip_blocks.append(returning)
                    if absorbs_back is not None:
                        subsumed.append(absorbs_back)
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
                subsumed=tuple(subsumed),
            )
        )

    return blocks, skipped, plans, held
