"""Match a long meeting journey to its latest timed TripIt flight or train.

Used only after an ordinary meeting route has exceeded its normal range, or
when a transport has left its meeting anchor unresolved. The existing scan
and itinerary continue to own local meeting origins. Matching is pure; the live
sweep maintains long-journey context through the existing decision owner.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from drive_decision import clear_meeting_route, meeting_key, record_meeting_route
from itinerary import Itinerary, parse_time
from meeting_source import DEFAULT_MAX_REASONABLE_DRIVE, MAX_ASSUMED_ROAD_TRIP
from rail_source import station_address

_AROUND_EVENT = timedelta(days=2)
_LOCAL_CONNECTION = timedelta(hours=3)


def booked_transport_to(
    *,
    destination: str,
    at: datetime,
    schedule: list[dict],
    itinerary: Itinerary,
    route: Callable[[str, str], timedelta | None],
) -> bool | None:
    """Whether the latest nearby journey reaches the meeting's area.

    Flight destinations use the sweep's merged itinerary, including live status.
    A train's LOCATION is its departure station; its actual ``Arrive`` line
    supplies the destination.
    A booking elsewhere, a later departure or an untimed row proves nothing
    about getting to this meeting. Return None when a candidate's arrival cannot
    be resolved, so the existing hold path preserves uncertainty. Ordinary local
    drives never call this.
    """
    transport = itinerary.latest_transport_before(at)
    if transport is None or at - transport.arrives > _AROUND_EVENT:
        return False
    if transport.destination is not None:
        arrival = f"{transport.destination} airport"
    else:
        rail = next(
            (
                row
                for row in schedule
                if isinstance(row, dict)
                and row.get("type") == "Rail"
                and parse_time(row.get("start")) == transport.departs
                and parse_time(row.get("end")) == transport.arrives
            ),
            None,
        )
        arrival = station_address(rail, arriving=True) if rail is not None else None
    if not arrival:
        return None
    connection = route(arrival, destination)
    if connection is None:
        return None
    return timedelta(0) <= connection <= _LOCAL_CONNECTION


def maintain_meeting_routes(
    meetings,
    *,
    route,
    booked: set[str],
    driving_to: dict,
    verdicts: dict,
    no_routes: set[tuple[str, str]],
    now,
):
    """Record just long meeting journeys in the existing decision file.

    Called after planning on a live sweep only. The shared route closure has
    already computed these endpoints, so this does not add Maps requests.
    An unavailable route keeps previous evidence rather than claiming a
    missing booking from a provider failure.
    """
    for meeting in meetings:
        identity = meeting.meeting_id
        if identity in booked or identity in driving_to:
            if meeting_key(identity) in verdicts:
                clear_meeting_route(identity)
            continue
        if not meeting.legs:
            if meeting.bucket in {"filtered", "skipped"}:
                if meeting_key(identity) in verdicts:
                    clear_meeting_route(identity)
            continue
        if not (meeting.start and meeting.end and meeting.location):
            continue
        endpoints = [
            (leg.origin, leg.destination)
            for leg in meeting.legs
            if leg.origin is not None and leg.destination is not None
        ]
        durations = [route(origin, destination) for origin, destination in endpoints]
        unroutable = any(pair in no_routes for pair in endpoints)
        if not durations or (any(duration is None for duration in durations) and not unroutable):
            continue
        longest = max((d for d in durations if d is not None), default=None)
        if longest is not None and longest <= DEFAULT_MAX_REASONABLE_DRIVE and not unroutable:
            if meeting_key(identity) in verdicts:
                clear_meeting_route(identity)
            continue
        gap = "маршрута для машины нет — нужен рейс или поезд" if unroutable else None
        if longest is not None and longest > MAX_ASSUMED_ROAD_TRIP:
            gap = "маршрут слишком долгий для поездки на машине — нужен рейс или поезд"
        record_meeting_route(
            identity,
            summary=meeting.summary,
            start=meeting.start,
            end=meeting.end,
            destination=meeting.location,
            drive_seconds=int(longest.total_seconds()) if longest is not None else None,
            gap_reason=gap,
            now=now,
        )


def travel_plan_changes(
    plan, current, *, booked: set[str], names: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """Descriptions keyed by the existing Calendar IDs apply must confirm."""
    old = {block.event_id: block for block in current if block.event_id is not None}
    changes = {}
    for deletion in plan.deletes:
        block = old.get(deletion.event_id)
        if (
            block is not None
            and block.identity in booked
            and block.identity in names
            and block.kind in {"meeting_outbound", "meeting_return"}
            and block.baseline_seconds is not None
            and block.baseline_seconds > DEFAULT_MAX_REASONABLE_DRIVE.total_seconds()
        ):
            identity = block.identity
            changes[deletion.event_id] = (
                identity,
                f"Removed assumed road trip for {names[identity]}: a flight or train is booked.",
            )
    for update in plan.updates:
        block = old.get(update.event_id)
        desired = update.desired
        if (
            block is not None
            and block.identity in names
            and block.kind in {"meeting_outbound", "meeting_return"}
            and block.baseline_seconds is not None
            and block.baseline_seconds > DEFAULT_MAX_REASONABLE_DRIVE.total_seconds()
            and desired.baseline_seconds <= DEFAULT_MAX_REASONABLE_DRIVE.total_seconds()
            and (block.origin != desired.origin or block.destination != desired.destination)
        ):
            identity = block.identity
            changes[update.event_id] = (
                identity,
                f"Updated drive for {names[identity]}: {desired.origin} → {desired.destination}.",
            )
    return changes
