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
   home is NEVER the anchor while a trip is active.

The schedule file is host-group state owned by `nightly-travel-sync` (see
its state-schema.md); this module is a non-owner READER per
`coding-policy: stateful-artifacts` — a missing, unreadable, malformed, or
forward-incompatible file resolves to "no usable schedule" (static-home
behavior), never an exception, and never a migration.

Shared across bundles: drive-planner's sweep precheck imports this module
cross-bundle the same way it already imports `maps_client` from this skill.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from trip_origin import (
        TripAnchor, flight_windows, load_travel_schedule, resolve_anchor,
    )

    schedule = load_travel_schedule()            # list | None, tolerant
    anchor = resolve_anchor(schedule, at=meeting_start, home_address=home)
    anchor.address    # drivable anchor, or None (unresolved mid-trip)
    anchor.source     # "home" | "lodging" | "trip_location" | "unresolved"
    flight_windows(schedule)  # [(start, end), ...] — flight spans to filter (#85)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

SCHEDULE_PATH = "/workspace/group/travel-schedule.json"

# Highest travel-schedule.json record schema this reader accepts. Bump in
# lock-step with refresh-travel-schedule.py's SCHEMA_VERSION per
# `coding-policy: stateful-artifacts`. Records without a schema_version are
# legacy pre-versioned records (written before the field existed) that this
# reader treats as v1; any record carrying a HIGHER version marks the whole
# file forward-incompatible — this reader is lagging, so it takes the
# no-usable-schedule path rather than guessing at a shape it doesn't know.
SCHEDULE_SCHEMA_VERSION = 1


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


def _parse_when(value) -> datetime | None:
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
    parsed = _parse_when(value)
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


def resolve_anchor(
    schedule: list[dict] | None,
    *,
    at: datetime,
    home_address: str | None,
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
    best = None
    best_when = None
    for record in schedule:
        if record.get("type") != "Lodging":
            continue
        location = record.get("location")
        if not isinstance(location, str) or not location.strip():
            continue
        when = _parse_when(record.get("start"))
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


def flight_windows(schedule: list[dict] | None) -> list[tuple[datetime, datetime]]:
    """UTC (start, end) spans for every timed `Flight` segment in the schedule.

    drive-planner's `scan` uses these to filter TripIt flight events out of
    ground-meeting classification (#85): a calendar event overlapping a flight
    window is air travel — owned by flight-assist — never a ground meeting to
    draw a drive block for (the London-hotel→JFK-layover "drive"). A None /
    empty schedule yields no windows (the pre-#85 flight-unaware behavior — no
    windows means no filtering, so a real meeting is never suppressed).

    Only a segment whose `start` and `end` both parse to instants AND both
    carry a time-of-day (`T` in the raw value) produces a window. A date-only
    or unparseable segment is skipped: a date-only "flight" would span whole
    calendar days and could suppress a real same-day meeting, so the safe
    direction is to emit no window for it. A non-positive span is skipped too.
    """
    if not schedule:
        return []
    windows: list[tuple[datetime, datetime]] = []
    for record in schedule:
        if record.get("type") != "Flight":
            continue
        raw_start = record.get("start")
        raw_end = record.get("end")
        if not (isinstance(raw_start, str) and "T" in raw_start):
            continue
        if not (isinstance(raw_end, str) and "T" in raw_end):
            continue
        start = _parse_when(raw_start)
        end = _parse_when(raw_end)
        if start is None or end is None or end <= start:
            continue
        windows.append((start, end))
    return windows


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
    anchor = resolve_anchor(load_travel_schedule(), at=now, home_address=home_address)
    return anchor.address
