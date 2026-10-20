"""Trip-aware drive anchors: the TripIt schedule read as one itinerary (#122, #335).

Every drive leg the travel skills plan is anchored somewhere: the sweep's
airport and meeting legs at the operator's position around the flight or the
meeting, flight-assist's time-to-leave and drive-home legs at "home". That
anchor used to be the static home, which drew a 39-minute drive block from a
Tennessee origin for a UK dinner (#122), and then a ladder of rules over the
TripIt schedule, which never saw a flight byAir alone tracked (#335).

The position now comes from `itinerary.Itinerary`: transports split time into
ground periods, lodging stays belong to the period they overlap most, and a
period at a home airport is home. This module wraps it for callers holding a
schedule, and keeps the schedule's reader and the flight helpers the meeting
scan uses.

The schedule file is host-group state owned by `nightly-travel-sync` (see
its state-schema.md); this module is a non-owner READER per
`coding-policy: stateful-artifacts` — a missing, unreadable, malformed, or
forward-incompatible file resolves to "no usable schedule" (static-home
behavior), never an exception, and never a migration.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from trip_origin import (
        TripAnchor, flight_summaries, flight_windows, home_airports,
        load_travel_schedule, resolve_anchor, resolve_effective_home,
    )

    schedule = load_travel_schedule()            # list | None, tolerant
    anchor = resolve_anchor(schedule, at=meeting_start, home_address=home,
                            home_airports=home_airports(), transports=byair)
    anchor.address    # drivable anchor, or None (unresolved away from home)
    anchor.source     # "home" | "lodging" | "trip_location" | "unresolved"
    flight_windows(schedule)    # [(start, end), ...] — flight spans to filter (#85)
    flight_summaries(schedule)  # ["DL 4908 ...", ...] — flight identities to filter (#85)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# Co-located in this bundle; import by module name so consumers that put the
# bundle on sys.path (see SKILL.md) resolve it the same way they resolve this
# module.
_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from addresses import read_values  # noqa: E402
from itinerary import Itinerary, Position, Transport, parse_time  # noqa: E402

SCHEDULE_PATH = "/workspace/group/travel-schedule.json"

# Highest travel-schedule.json record schema this reader accepts. Bump in
# lock-step with refresh-travel-schedule.py's SCHEMA_VERSION per
# `coding-policy: stateful-artifacts`. Records without a schema_version are
# legacy pre-versioned records (written before the field existed) that this
# reader treats as v1; any record carrying a HIGHER version marks the whole
# file forward-incompatible — this reader is lagging, so it takes the
# no-usable-schedule path rather than guessing at a shape it doesn't know.
SCHEDULE_SCHEMA_VERSION = 5

# The anchor is an itinerary position: `address`, `source`, `detail`.
TripAnchor = Position


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
    return parse_time(value)


def home_airports(*, path: Path | None = None) -> frozenset[str]:
    """The profile's `home_airport` codes (`addresses.read_values`), uppercased.

    Empty when the profile has none: the itinerary then judges home by the trip
    covering an instant rather than by airport (see `itinerary`)."""
    return frozenset(
        code.strip().upper() for code in read_values("home_airport", path=path) if code
    )


def resolve_anchor(
    schedule: list[dict] | None,
    *,
    at: datetime,
    home_address: str | None,
    home_airports: frozenset[str] = frozenset(),
    transports: list[Transport] | tuple[Transport, ...] = (),
) -> TripAnchor:
    """The operator's position at `at` (tz-aware; naive raises ValueError).

    One-shot convenience over `Itinerary`; a caller asking many instants of one
    run builds the `Itinerary` once instead. `home_address` may be None (the
    off-trip anchor then has no address, source "home"). `transports` are the
    flights or trains known beyond the schedule (the drive engine passes
    byAir's).
    """
    return Itinerary(
        schedule,
        home_address=home_address,
        home_airports=home_airports,
        transports=transports,
    ).position_at(at)


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


def resolve_effective_home(
    home_address: str | None,
    *,
    now: datetime,
    transports: list[Transport] | tuple[Transport, ...] = (),
) -> str | None:
    """The trip-aware stand-in for the static `home_address` at `now`.

    The I/O convenience for callers that treat "home" as a single per-cycle
    value (flight-assist's time-to-leave origin and drive-home destination):
    reads the schedule and the profile's home airports, and returns the
    position's address — the static home at home, the current lodging or trip
    location away, None when away with nothing resolvable (the callers'
    existing "no home_address configured" handling then skips routing, which
    beats routing to a residence an ocean away).
    """
    return resolve_anchor(
        load_travel_schedule(),
        at=now,
        home_address=home_address,
        home_airports=home_airports(),
        transports=transports,
    ).address
