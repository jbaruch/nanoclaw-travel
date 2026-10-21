"""Local wall-clock reconstruction for TripIt ICS records.

TripIt's ICS feed stamps every timed VEVENT in UTC (`DTSTART:20260823T060500Z`)
and never emits a `TZID`. A traveller's day is local, so a red-eye leaving San
Francisco at 11:05 PM on Aug 22 arrives in the feed as an Aug 23 event, and
every consumer that reduces the instant to a calendar date reads the departure
a day late. That is what made the booking checker report the night of Aug 22 as
a missing hotel: the night was spent on the plane, and the plane's departure
had been filed under the 23rd.

The local clock is not lost. DESCRIPTION renders the itinerary the way TripIt
displays it, and every timed record prints its local time:

    11:05 PM PDT
    [Flight] SFO to BNA
    ...
    Sun\\, Aug 23
    5:30 AM CDT
    Arrive Nashville (BNA)

A printed wall clock plus the known UTC instant determines the UTC offset: the
difference between the two clocks is the offset modulo 24h, and exactly one
candidate normally lands inside the real-world offset range
[-12:00, +14:00]. That is arithmetic, not a timezone lookup — no zone
database, no airport-to-zone table, no network call.

Reconstruction fails CLOSED. A record whose local time is missing,
unparseable, or genuinely ambiguous yields None, and the caller emits no
local field, leaving consumers on the UTC dates they already used. A wrong
local date would move a night; a missing one only preserves today's behavior.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from tripit_local_time import local_times

    local_times(start_utc, end_utc, description)  # → (datetime|None, datetime|None)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# The real-world UTC offset range. Earth's inhabited offsets run from -12:00
# (Baker Island) to +14:00 (Line Islands), a 26-hour span — two hours wider
# than the 24-hour period the clock arithmetic resolves. Offsets in
# [-12:00, -10:00] therefore share a printed clock with [+12:00, +14:00]
# (Honolulu 14:00 and Kiritimati 14:00 on consecutive dates are the same UTC
# instant). Those records resolve to two candidates and are refused rather
# than guessed — see `offset_minutes`.
MIN_OFFSET_MINUTES = -12 * 60
MAX_OFFSET_MINUTES = 14 * 60

_MINUTES_PER_DAY = 24 * 60

# A rendered clock line, e.g. `11:05 PM PDT` or `6:20 AM CDT`. The zone
# abbreviation is captured but never interpreted — the offset comes from the
# arithmetic, so a feed that drops the abbreviation still resolves. Anchored at
# both ends so a line that merely CONTAINS a time (`Check-In: 10:00pm`, which
# TripIt writes without the space and in lower case) never matches.
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s+(AM|PM)(?:\s+\S+)?$")

# TripIt's day divider, e.g. `Sun, Aug 23`. Skipped while scanning backwards
# for a record's clock line: a divider sits between the time and the entry it
# belongs to whenever the rendered day changes.
_DAY_HEADER_RE = re.compile(r"^[A-Za-z]{3,9},\s+[A-Za-z]{3,9}\.?\s+\d{1,2}$")

# The `[Flight]` / `[Lodging]` / `[Car Rental]` entry marker. The clock line
# for the record's START is the last one above it.
_ENTRY_RE = re.compile(r"^\[[^\]]+\]")

# The arrival half of a segment that moves between zones. Its own clock line
# is printed in the DESTINATION's zone, so it resolves to its own offset.
# Anchored at line start on purpose: TripIt writes lodging as
# `[Lodging] Arrive <hotel>`, one location and one clock, which must not be
# read as a second zone.
_ARRIVE_RE = re.compile(r"^Arrive\b")

# RFC 5545 TEXT escapes, as they survive in the raw DESCRIPTION value.
_UNESCAPE = ((r"\n", "\n"), (r"\N", "\n"), (r"\,", ","), (r"\;", ";"), ("\\\\", "\\"))


def _unescape(text: str) -> str:
    """Decode the RFC 5545 TEXT escapes in a raw DESCRIPTION value."""
    for escaped, plain in _UNESCAPE:
        text = text.replace(escaped, plain)
    return text


def offset_minutes(instant: datetime, hour: int, minute: int) -> int | None:
    """The UTC offset a printed local clock implies for a known UTC instant.

    Returns the unique offset in [-12:00, +14:00] whose application to
    `instant` yields the wall clock `hour:minute`, or None when no candidate
    or more than one candidate qualifies. `instant` must be UTC.
    """
    base = (hour * 60 + minute) - (instant.hour * 60 + instant.minute)
    candidates = [
        candidate
        for candidate in (base - _MINUTES_PER_DAY, base, base + _MINUTES_PER_DAY)
        if MIN_OFFSET_MINUTES <= candidate <= MAX_OFFSET_MINUTES
    ]
    return candidates[0] if len(candidates) == 1 else None


def _clock(line: str) -> tuple[int, int] | None:
    """`(hour, minute)` on a 24-hour clock for a rendered time line, else None."""
    match = _TIME_RE.match(line)
    if match is None:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3) == "PM":
        hour += 12
    minute = int(match.group(2))
    if minute > 59:
        return None
    return hour, minute


def _clock_above(lines: list[str], index: int) -> tuple[int, int] | None:
    """The clock line governing the entry at `lines[index]`, else None.

    Scans upward past blank lines and day dividers only. Any other content
    means the rendering is not the shape this module knows how to read, and
    the scan gives up rather than reaching further for a time that belongs to
    a different entry.
    """
    for line in reversed(lines[:index]):
        clock = _clock(line)
        if clock is not None:
            return clock
        if line and not _DAY_HEADER_RE.match(line):
            return None
    return None


def _apply(instant: datetime, offset: int) -> datetime:
    """`instant` rendered in the zone `offset` minutes from UTC."""
    return instant.astimezone(timezone(timedelta(minutes=offset)))


def local_times(
    start: datetime, end: datetime, description: str
) -> tuple[datetime | None, datetime | None]:
    """Local start and end for a timed TripIt record, each None when unresolved.

    `start` and `end` are the record's UTC instants; `description` is the raw
    DESCRIPTION value, escapes included. Each returned datetime is the same
    instant carrying the reconstructed offset, so `.isoformat()` renders the
    traveller's own date and clock.

    Each half resolves against its OWN printed clock and never inherits the
    other's offset. A segment landing in another zone prints a second clock
    under an `Arrive` line, and that clock is the arrival's authority. A
    record with one location and one clock (lodging, a car rental) gets a
    start stamp and no end stamp: TripIt pads those to a synthetic one-hour
    DTEND it never renders, so no printed clock stands behind the end, and
    carrying the start's offset over would assert an offset that could have
    changed in between (a DST transition inside the span). One stay is two
    records here, check-in and check-out, each printing its own clock, so the
    end an unstamped record leaves behind is one nothing needed.
    """
    lines = [line.strip() for line in _unescape(description).splitlines()]

    entry = next((i for i, line in enumerate(lines) if _ENTRY_RE.match(line)), None)
    if entry is None:
        return None, None
    departure = _clock_above(lines, entry)
    if departure is None:
        return None, None
    start_offset = offset_minutes(start, *departure)
    if start_offset is None:
        return None, None
    local_start = _apply(start, start_offset)

    arrival_line = next((i for i, line in enumerate(lines) if _ARRIVE_RE.match(line)), None)
    if arrival_line is None:
        return local_start, None
    arrival = _clock_above(lines, arrival_line)
    if arrival is None:
        return local_start, None
    end_offset = offset_minutes(end, *arrival)
    if end_offset is None:
        return local_start, None
    return local_start, _apply(end, end_offset)
