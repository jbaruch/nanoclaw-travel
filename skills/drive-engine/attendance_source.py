"""Explicit day windows into the existing position timeline and reconcile owner.

Pure: validated attendance rows, itinerary, clock and route are supplied. No
state, provider, approval or Calendar authority is introduced here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from conference_attendance import Attendance
from itinerary import ATTENDANCE, AttendancePresence, AttendanceScope, Itinerary
from meeting_source import DEFAULT_MAX_REASONABLE_DRIVE, MIN_MEETING_DRIVE
from reconcile import DesiredBlock, HeldLeg

RouteFn = Callable[[str, str], timedelta | None]


def occurrence_scopes(rows: tuple[Attendance, ...]) -> tuple[AttendanceScope, ...]:
    return tuple(
        AttendanceScope(
            row.id,
            datetime.combine(
                date.fromisoformat(row.starts_on), time.min, ZoneInfo(row.time_zone)
            ).astimezone(UTC),
            datetime.combine(
                date.fromisoformat(row.ends_on) + timedelta(days=1),
                time.min,
                ZoneInfo(row.time_zone),
            ).astimezone(UTC),
        )
        for row in rows
        if row.schema_version == 2
    )


def presence_windows(rows: tuple[Attendance, ...]) -> tuple[AttendancePresence, ...]:
    return tuple(
        AttendancePresence(row.id, start, end, day_end, row.venue_location)
        for row in rows
        if row.schema_version == 2 and row.venue_location is not None
        for day in row.days
        for start, end, day_end in (day.bounds(row.time_zone),)
    )


def attendance_desired_blocks(
    rows: tuple[Attendance, ...],
    *,
    itinerary: Itinerary,
    route: RouteFn,
    now: datetime,
    window: timedelta,
    busy_windows: Sequence[tuple[datetime, datetime]] | None = (),
) -> tuple[list[DesiredBlock], list[str], list[HeldLeg]]:
    """One pair per explicit day; released lodging deliberately has no return.

    An unknown endpoint/route or a conflicting Calendar interval holds its own
    existing leg. An explicit withdrawn day is absent and may be reconciled out.
    Occurrence ID + date is encoded as an unambiguous pair, not a Calendar ID.
    """
    import json

    blocks: list[DesiredBlock] = []
    skipped: list[str] = []
    held: list[HeldLeg] = []
    for row in rows:
        if row.schema_version != 2 or row.venue_location is None:
            continue
        for day in row.days:
            start, end, _ = day.bounds(row.time_zone)
            if end < now or start > now + window:
                continue
            identity = json.dumps([row.id, day.date], ensure_ascii=False, separators=(",", ":"))
            for returning, anchor in ((False, start), (True, end)):
                kind = "attendance_return" if returning else "attendance_outbound"
                summary = f"Drive: {'from' if returning else 'to'} conference venue"
                base = itinerary.base_at(
                    anchor if returning else anchor - timedelta(microseconds=1)
                )
                # Reuse the shared actual transport/presence precedence. A later
                # ground period cannot inherit an earlier window's venue merely
                # because the owner's accepted clock interval has not ended.
                presence = itinerary.position_at(
                    anchor - timedelta(microseconds=1) if returning else anchor
                )
                reason = None
                drive = None
                if (
                    presence.source != ATTENDANCE
                    or presence.detail != row.id
                    or presence.address != row.venue_location
                ):
                    reason = "attendance commute venue presence invalidated by transport"
                elif returning and base.address is None and itinerary.lodging_checked_out(anchor):
                    continue
                elif anchor < now:
                    reason = "attendance commute is past; retain prior leg"
                elif busy_windows is None:
                    reason = "attendance commute Calendar coverage is unknown"
                elif base.address is None:
                    reason = "attendance commute has no grounded available base"
                else:
                    origin, destination = (
                        (row.venue_location, base.address)
                        if returning
                        else (base.address, row.venue_location)
                    )
                    drive = route(origin, destination)
                    if (
                        drive is None
                        or drive > DEFAULT_MAX_REASONABLE_DRIVE
                        or drive < timedelta(0)
                    ):
                        reason = "attendance commute route unavailable or implausible"
                    elif drive <= MIN_MEETING_DRIVE:
                        continue
                    else:
                        depart, arrive = (
                            (anchor, anchor + drive) if returning else (anchor - drive, anchor)
                        )
                        base_instant = arrive if returning else depart
                        if depart < now:
                            reason = "attendance commute has already begun; retain prior leg"
                        elif itinerary.base_at(base_instant).address != base.address:
                            reason = "attendance commute crosses lodging/transport availability"
                        elif any(a < arrive and depart < b for a, b in busy_windows):
                            reason = "attendance commute conflicts with an actual Calendar interval"
                if reason is not None:
                    skipped.append(reason)
                    held.append(
                        HeldLeg(
                            identity,
                            kind,
                            summary,
                            anchor,
                            reason,
                            not_before=anchor if returning else None,
                        )
                    )
                else:
                    assert drive is not None and base.address is not None
                    blocks.append(
                        DesiredBlock(
                            identity=identity,
                            kind=kind,
                            summary=summary,
                            start=anchor if returning else anchor - drive,
                            end=anchor + drive if returning else anchor,
                            origin=row.venue_location if returning else base.address,
                            destination=base.address if returning else row.venue_location,
                            baseline_seconds=int(drive.total_seconds()),
                            anchor=anchor,
                            timezone=row.time_zone,
                        )
                    )
    return blocks, skipped, held
