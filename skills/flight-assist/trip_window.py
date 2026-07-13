"""Trip-window evaluation — the flight-assist precheck's defense-in-depth (#147).

The host owns the primary control: a pre-spawn gate (`src/spawn-gates.ts`,
jbaruch/nanoclaw#754) that reads the group's `travel-db.json` and does NOT spawn
the flight-assist container outside a trip window, so the `*/2` cadence costs no
container off-trip. This module is the belt-and-suspenders: if a container is
somehow spawned off-window anyway, the precheck consults the SAME file with the
SAME formula and bails before any byAir call.

Single source of truth: `travel-db.json` is written by this plugin's
`check-travel-bookings` skill (`scripts/build-travel-db.py`, via
`nightly-travel-sync`) and read by the host gate. This reader mirrors the host's
window definition byte-for-byte so the two layers can never disagree — no second
trip store. Schema: `skills/check-travel-bookings/state-schema.md`.

Window (v1, mirrors the host): a trip covers `now` when
    (start − 24h) ≤ now < (end + 24h)
with `start`/`end` bare `YYYY-MM-DD` dates read as UTC midnight; the +24h trail
keeps the operator in-window through the whole final day. Union over all trips.

Fail behaviour is deliberately asymmetric (flight-assist is safety-relevant —
missing a gate change mid-trip is worse than a wasted poll), matching the host:
  - file ABSENT            → out of window (no itinerary → nothing to act on)
  - present but UNREADABLE
    / not JSON / wrong shape → in window (fail OPEN) so a corrupt file never
                               blinds an active trip
  - present + VALID        → evaluate trips; a trip with unparseable dates is
                             skipped, an empty trip map is out of window

Unlike the host gate, this reader does NOT do realpath symlink-containment: that
guards a HOST-side read of a container-writable file against a planted symlink
escaping to the host filesystem. This reader already runs INSIDE the container
sandbox reading its own group volume, so that threat does not apply.

stdlib-only (`json`, `datetime`, `pathlib`) per `coding-policy:
dependency-management`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# In-container mount of the group volume; the same file the host gate reads at
# `<groupDir>/travel-db.json` and the same one `build-travel-db.py` writes.
TRAVEL_DB_PATH = "/workspace/group/travel-db.json"

# Test override for the travel-db location, mirroring FLIGHT_ASSIST_STATE_DIR.
# Production leaves it unset and reads the mount path above.
_TRAVEL_DB_ENV = "FLIGHT_ASSIST_TRAVEL_DB"

# The window opens 24h before a trip's start date and closes 24h after its end
# date (the trail makes the bare `end` date inclusive through end-of-day UTC).
_TRIP_WINDOW_LEAD = timedelta(hours=24)
_TRIP_WINDOW_TRAIL = timedelta(hours=24)


@dataclass(frozen=True)
class TripWindow:
    """Verdict for one cycle. `in_window` gates the precheck; `reason` is logged."""

    in_window: bool
    reason: str


def _parse_db_date(value: object) -> datetime | None:
    """Parse a `travel-db.json` `start`/`end` value to a UTC instant, or None.

    The documented shape is a bare `YYYY-MM-DD` (read as UTC midnight, matching
    the host's `Date.parse`). A full ISO datetime is tolerated, including a
    trailing `Z` (normalized to `+00:00` so a `Z`-stamped writer never falls out
    of window on Python builds whose `fromisoformat` predates `Z` support).
    Anything else — non-string, unparseable — returns None so the caller skips
    that trip, exactly as the host skips a trip whose dates don't parse.
    """
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def evaluate_trip_window(*, now_utc: datetime, path: str | None = None) -> TripWindow:
    """Decide whether `now_utc` falls in any trip's window per `travel-db.json`.

    `now_utc` is injected (never read from the clock here) so tests pin it.
    `path` overrides the travel-db location for tests; production uses the
    module default (or the `FLIGHT_ASSIST_TRAVEL_DB` env override).
    """
    db_path = Path(path or os.environ.get(_TRAVEL_DB_ENV) or TRAVEL_DB_PATH)

    try:
        raw = db_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # No itinerary on disk → nothing for a windowed skill to act on. This is
        # the steady off-trip state; suppressing is correct, not blinding.
        return TripWindow(False, "no travel itinerary on disk — out of window")
    except OSError as read_err:
        # Present but unreadable (perms, a directory in its place, a dangling
        # symlink). Fail OPEN — a broken file must never blind an active trip.
        return TripWindow(True, f"travel-db unreadable ({read_err}) — failing open")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as parse_err:
        return TripWindow(True, f"travel-db not valid JSON ({parse_err}) — failing open")

    trips = parsed.get("trips") if isinstance(parsed, dict) else None
    if not isinstance(trips, dict):
        # Missing `trips` key or a non-object (array / primitive) is a truncated
        # or corrupt shape — fail OPEN rather than blind a possibly-active trip.
        return TripWindow(True, "travel-db missing a well-formed trips map — failing open")

    # A valid (possibly empty) trip map. An empty map covers nothing → out.
    for trip_id, trip in trips.items():
        if not isinstance(trip, dict):
            continue
        start = _parse_db_date(trip.get("start"))
        end = _parse_db_date(trip.get("end"))
        if start is None or end is None:
            # A trip whose dates don't parse can't be meaningfully active — skip
            # it, matching the host, rather than fail the whole evaluation.
            continue
        if start - _TRIP_WINDOW_LEAD <= now_utc < end + _TRIP_WINDOW_TRAIL:
            return TripWindow(True, f"in trip window {trip_id!r} (24h lead)")

    return TripWindow(False, "no trip window covers now — out of window")
