"""Tests for position_at (pure planned position) and the GPS-overlay resolver.

Deterministic fixtures only — fixed tz-aware datetimes and hand-built schedule
records, no wall-clock. These pin the #156 R1 split: position_at is pure and
itinerary-only (delegates to trip_origin's lodging→trip→home ladder), while the
live-GPS overlay lives entirely in resolve_leg_origin and fires only inside the
imminence window.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from position import (  # noqa: E402
    GPS_IMMINENCE_MARGIN,
    LIVE_GPS,
    ResolvedOrigin,
    is_drive_imminent,
    position_at,
    resolve_leg_origin,
)
from trip_origin import TripAnchor  # noqa: E402

UTC = timezone.utc
HOME = "12 Example St, Sampleton, TN 37000"
HOTEL = "Hotel Skt Petri, Copenhagen"


def _dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# --- position_at: pure delegation to the lodging ladder ---------------------


def test_position_at_off_trip_is_home():
    anchor = position_at(None, _dt(2020, 7, 12, 9), home_address=HOME)
    assert anchor.address == HOME
    assert anchor.source == "home"


def test_position_at_on_trip_resolves_lodging():
    schedule = [
        {"type": "Trip", "start": "2020-07-11", "end": "2020-07-15", "summary": "CPH"},
        {
            "type": "Lodging",
            "start": "2020-07-11T14:00:00+00:00",
            "location": HOTEL,
            "summary": "Skt Petri",
        },
    ]
    # A leave_by the morning after check-in resolves to the hotel — the #154 case.
    anchor = position_at(schedule, _dt(2020, 7, 12, 6), home_address=HOME)
    assert anchor.address == HOTEL
    assert anchor.source == "lodging"


def test_position_at_before_checkin_is_not_the_hotel():
    schedule = [
        {"type": "Trip", "start": "2020-07-11", "end": "2020-07-15", "summary": "CPH"},
        {
            "type": "Lodging",
            "start": "2020-07-12T14:00:00+00:00",
            "location": HOTEL,
            "summary": "Skt Petri",
        },
    ]
    # An instant BEFORE the check-in must not anchor at that hotel (falls to trip
    # location / unresolved, never the future lodging).
    anchor = position_at(schedule, _dt(2020, 7, 12, 6), home_address=HOME)
    assert anchor.source != "lodging"


def test_position_at_rejects_naive_instant():
    with pytest.raises(ValueError, match="timezone-aware"):
        position_at(None, datetime(2020, 7, 12, 9), home_address=HOME)


# --- is_drive_imminent: the window boundary ---------------------------------

LEAVE_BY = _dt(2020, 7, 12, 9, 0)
DRIVE = timedelta(minutes=30)


def test_not_imminent_a_day_ahead():
    assert not is_drive_imminent(_dt(2020, 7, 11, 9), LEAVE_BY, DRIVE)


def test_imminent_at_lower_boundary():
    # Activates exactly drive + margin before leave_by.
    now = LEAVE_BY - (DRIVE + GPS_IMMINENCE_MARGIN)
    assert is_drive_imminent(now, LEAVE_BY, DRIVE)


def test_not_imminent_just_before_boundary():
    now = LEAVE_BY - (DRIVE + GPS_IMMINENCE_MARGIN) - timedelta(minutes=1)
    assert not is_drive_imminent(now, LEAVE_BY, DRIVE)


def test_imminent_at_and_past_leave_by():
    assert is_drive_imminent(LEAVE_BY, LEAVE_BY, DRIVE)
    assert is_drive_imminent(LEAVE_BY + timedelta(minutes=5), LEAVE_BY, DRIVE)


def test_imminent_rejects_naive():
    with pytest.raises(ValueError, match="timezone-aware"):
        is_drive_imminent(datetime(2020, 7, 12, 9), LEAVE_BY, DRIVE)


def test_imminent_rejects_negative_drive():
    with pytest.raises(ValueError, match="non-negative"):
        is_drive_imminent(LEAVE_BY, LEAVE_BY, timedelta(minutes=-1))


# --- resolve_leg_origin: overlay only inside the window ----------------------

PLANNED = TripAnchor(address=HOTEL, source="lodging", detail="Skt Petri")
LIVE = "55.6761,12.5683"


def test_plan_wins_when_not_imminent_even_with_fresh_fix():
    resolved = resolve_leg_origin(
        PLANNED,
        now=_dt(2020, 7, 11, 9),  # a day ahead
        leave_by=LEAVE_BY,
        drive=DRIVE,
        live_origin=LIVE,
    )
    assert resolved == ResolvedOrigin(address=HOTEL, source="lodging")


def test_gps_overlay_wins_when_imminent():
    resolved = resolve_leg_origin(
        PLANNED,
        now=LEAVE_BY - timedelta(minutes=10),  # inside the window
        leave_by=LEAVE_BY,
        drive=DRIVE,
        live_origin=LIVE,
    )
    assert resolved == ResolvedOrigin(address=LIVE, source=LIVE_GPS)


def test_plan_wins_when_imminent_but_no_fresh_fix():
    resolved = resolve_leg_origin(
        PLANNED,
        now=LEAVE_BY - timedelta(minutes=10),
        leave_by=LEAVE_BY,
        drive=DRIVE,
        live_origin=None,  # stale / absent, caller passed None
    )
    assert resolved == ResolvedOrigin(address=HOTEL, source="lodging")


def test_unresolved_plan_passes_through_when_not_imminent():
    unresolved = TripAnchor(address=None, source="unresolved")
    resolved = resolve_leg_origin(
        unresolved,
        now=_dt(2020, 7, 11, 9),
        leave_by=LEAVE_BY,
        drive=DRIVE,
        live_origin=None,
    )
    assert resolved.address is None
    assert resolved.source == "unresolved"
