"""Booked TripIt trains → station drives in the existing calendar reconcile.

Uses the same itinerary bases, routing, holds and post-arrival composition as
flights. No provider calls, state file, cadence or fabricated flight records.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from itinerary import TRIP_LOCATION, Itinerary, parse_time
from meeting_source import MAX_ASSUMED_ROAD_TRIP, MIN_MEETING_DRIVE
from postarrival import TransportGuard
from reconcile import DesiredBlock, HeldLeg
from scan import same_place

# Default boarding allowance. Amtrak's ordinary station guidance is 30 minutes:
# https://www.amtrak.com/at-the-station/boarding.html
STATION_ARRIVAL_BUFFER = timedelta(minutes=30)


def station_address(row: dict, *, arriving: bool) -> str | None:
    """TripIt's observed Depart/Arrive line, not LOCATION (departure only)."""
    text = row.get("description")
    if not isinstance(text, str):
        return None
    text = text.replace("\\n", "\n").replace("\\,", ",")
    prefix = "Arrive " if arriving else "Depart "
    return next(
        (
            line[len(prefix) :].strip()
            for line in text.splitlines()
            if line.startswith(prefix) and line[len(prefix) :].strip()
        ),
        None,
    )


def _base(itinerary: Itinerary, at: datetime) -> str | None:
    position = itinerary.position_at(at)
    if position.source == TRIP_LOCATION:
        lodging = itinerary.addressable_lodging_in_period(at)
        return lodging.address if lodging is not None else None
    return position.address


def station_desired_blocks(
    schedule,
    *,
    itinerary: Itinerary,
    route,
    now: datetime,
    window: timedelta,
    display_tz: str | None = None,
):
    """Station legs and real transport guards, folded into the one reconcile."""
    desired: list[DesiredBlock] = []
    held: list[HeldLeg] = []
    skipped: list[str] = []
    guards: list[TransportGuard] = []
    for row in schedule or []:
        if not isinstance(row, dict) or row.get("type") != "Rail":
            continue
        identity = row.get("uid")
        start, end = parse_time(row.get("start")), parse_time(row.get("end"))
        if (
            not isinstance(identity, str)
            or not identity
            or start is None
            or end is None
            or end <= start
            or not all("T" in str(row.get(k)) for k in ("start", "end"))
        ):
            raise ValueError(
                "TripIt Rail needs its UID and timed start/end — refresh travel-schedule"
            )
        if end < now or start > now + window:
            continue
        identity = f"rail:{identity}"
        departure = station_address(row, arriving=False)
        arrival = station_address(row, arriving=True)
        board_at = start - STATION_ARRIVAL_BUFFER
        guards.append(
            TransportGuard(
                identity=identity,
                guard_start=board_at,
                ready_at=end,
                arrival_name=arrival or str(row.get("summary", "train")),
                arrival_point=arrival or "",
                arrival_label=arrival or "",
                arrival_kind="station_arrival",
            )
        )
        for kind, at, origin, destination in (
            ("station_departure", board_at, _base(itinerary, board_at), departure),
            ("station_arrival", end, arrival, _base(itinerary, end)),
        ):
            if at < now:
                continue
            reason = None
            drive = None
            if not origin or not destination:
                reason = "station address or grounded home/lodging base unavailable"
            elif same_place(origin, destination):
                continue
            else:
                drive = route(origin, destination)
                if drive is None:
                    reason = "station drive route unavailable"
                elif drive < timedelta(0) or drive > MAX_ASSUMED_ROAD_TRIP:
                    reason = "station drive outside the road-trip range"
                elif drive <= MIN_MEETING_DRIVE:
                    continue
            summary = f"Drive: {row.get('summary', 'Train')}"
            if reason is not None:
                held.append(
                    HeldLeg(
                        identity,
                        kind,
                        summary,
                        at,
                        reason,
                        not_before=at if kind == "station_arrival" else None,
                    )
                )
                skipped.append(f"{kind} {identity}: {reason}")
                continue
            assert drive is not None and origin is not None and destination is not None
            arriving = kind == "station_arrival"
            desired.append(
                DesiredBlock(
                    identity=identity,
                    kind=kind,
                    summary=summary,
                    start=at if arriving else at - drive,
                    end=at + drive if arriving else at,
                    origin=origin,
                    destination=destination,
                    baseline_seconds=int(drive.total_seconds()),
                    anchor=at,
                    timezone=display_tz,
                    zone_from_operator=bool(display_tz),
                )
            )
    return desired, held, skipped, guards
