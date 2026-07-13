"""position_at — the pure planned-position primitive — plus the GPS-overlay origin
resolver (#156 R1).

R1 splits two concerns the old engines tangled, and the split is the whole point:

- `position_at(schedule, T)` — the PLANNED position at instant T: most-recent
  lodging check-in ≤ T, else trip location, else the static home off-trip. Pure —
  no clock, no GPS, deterministic. It is `trip_origin.resolve_anchor` bound to the
  engine's contract; every caller passes the CORRECT instant (a leg's `leave_by`
  or `depart_after`), so the "which `now` did the caller pass" scatter that caused
  #154 — resolving the origin at cycle `now` instead of at the drive's own time —
  cannot recur.
- `resolve_leg_origin(planned, now, …)` — overlays a fresh live-GPS fix onto the
  planned origin ONLY when the drive is imminent: `now` within
  `[drive + GPS_IMMINENCE_MARGIN]` of `leave_by`. Outside that window the plan
  wins — current GPS is irrelevant a day ahead, the operator is not there yet.
  GPS never enters `position_at`, so the primitive stays referentially transparent
  and unit-testable; the clock-dependence lives here, in a named wrapper.

Freshness of the live fix (the `MAX_LIVE_ORIGIN_AGE_MINUTES` cap) is applied by the
I/O layer that reads `current-location.json`; this module receives an
already-fresh origin string or `None`, and decides only the imminence question.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# trip_origin ships in the co-located travel-core bundle; resolve it at the runtime
# mount, dev-clone sibling fallback for CI (same cross-bundle pattern as the other
# consumers).
_BUNDLE_DIR = Path(__file__).resolve().parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from trip_origin import TripAnchor, resolve_anchor  # noqa: E402

# How close to leave-by the drive must be before a fresh live-GPS fix overrides the
# plan. Revisit-later default per #156 Decision 3.
GPS_IMMINENCE_MARGIN = timedelta(minutes=40)

LIVE_GPS = "live_gps"


def position_at(
    schedule: list[dict] | None, at: datetime, *, home_address: str | None
) -> TripAnchor:
    """The planned position at instant `at` — pure, itinerary-only (#156 R1).

    Delegates to `trip_origin.resolve_anchor`: lodging ladder → trip location →
    home. No clock, no GPS. `at` must be tz-aware. The engine passes the leg's own
    instant (`leave_by` for a departure, `depart_after` for an arrival), never
    cycle `now`.
    """
    return resolve_anchor(schedule, at=at, home_address=home_address)


@dataclass(frozen=True)
class ResolvedOrigin:
    """A leg origin after the imminence decision. `source` is `live_gps` when the
    fresh fix won, else the planned anchor's source (home / lodging / …)."""

    address: str | None
    source: str


def is_drive_imminent(
    now: datetime,
    leave_by: datetime,
    drive: timedelta,
    *,
    margin: timedelta = GPS_IMMINENCE_MARGIN,
) -> bool:
    """Whether `now` is close enough to `leave_by` for live GPS to matter (#156 R1).

    Activates once `now` reaches within `drive + margin` before `leave_by` and
    stays active through it — before that the operator is not yet positioned for
    this drive, so the plan wins. All three instants must be tz-aware.
    """
    if now.tzinfo is None or leave_by.tzinfo is None:
        raise ValueError("is_drive_imminent: now and leave_by must be timezone-aware")
    if drive < timedelta(0) or margin < timedelta(0):
        raise ValueError("is_drive_imminent: drive and margin must be non-negative")
    return now >= leave_by - (drive + margin)


def resolve_leg_origin(
    planned: TripAnchor,
    *,
    now: datetime,
    leave_by: datetime,
    drive: timedelta,
    live_origin: str | None,
    margin: timedelta = GPS_IMMINENCE_MARGIN,
) -> ResolvedOrigin:
    """Resolve a leg's origin: planned position, GPS-overlaid only when imminent.

    `planned` is `position_at(leave_by)`. `live_origin` is an already-fresh
    `"<lat>,<lng>"` string (the I/O layer applied the freshness cap) or `None`.
    The overlay applies only inside the imminence window; outside it, or with no
    fresh fix, the planned origin wins. Pure.
    """
    if live_origin is not None and is_drive_imminent(now, leave_by, drive, margin=margin):
        return ResolvedOrigin(address=live_origin, source=LIVE_GPS)
    return ResolvedOrigin(address=planned.address, source=planned.source)
