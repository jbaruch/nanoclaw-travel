"""Tests for TripIt Flight-segment → Flight parsing (R2 union feed).

Deterministic fixtures only — hand-built schedule segments matching the iCal shape
(`[Flight] ATL to SJO` in description, designator in summary), no wall-clock. These
pin the bounded parse: a segment with a parseable route + start becomes a TripIt
Flight; a non-Flight row, a route-less row, or an unparseable start is skipped
rather than guessed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from flight_identity import TRIPIT  # noqa: E402
from tripit_flights import flights_from_schedule  # noqa: E402

UTC = timezone.utc


def _seg(**over):
    seg = {
        "schema_version": 1,
        "type": "Flight",
        "uid": "uid-1",
        "summary": "DL 4908 ATL to SJO",
        "description": "[Flight] ATL to SJO",
        "start": "2020-07-12T09:00:00Z",
        "end": "2020-07-12T14:30:00Z",
        "location": "Hartsfield-Jackson Atlanta International Airport (ATL)",
    }
    seg.update(over)
    return seg


def test_parses_route_code_and_times():
    flights = flights_from_schedule([_seg()])
    assert len(flights) == 1
    f = flights[0]
    assert f.source == TRIPIT
    assert f.dep_airport == "ATL" and f.arr_airport == "SJO"
    assert f.code == "DL4908"
    assert f.scheduled_dep == datetime(2020, 7, 12, 9, 0, tzinfo=UTC)
    assert f.scheduled_arr == datetime(2020, 7, 12, 14, 30, tzinfo=UTC)
    assert f.tripit_segment_id == "uid-1"


def test_route_from_summary_when_description_absent():
    flights = flights_from_schedule([_seg(description=None, summary="AA100 JFK to LHR")])
    assert flights[0].dep_airport == "JFK" and flights[0].arr_airport == "LHR"


def test_non_flight_segment_skipped():
    assert flights_from_schedule([_seg(type="Lodging")]) == []


def test_routeless_segment_skipped():
    # No "XXX to YYY" anywhere → not guessed.
    assert (
        flights_from_schedule([_seg(summary="Dinner reservation", description="table for 2")]) == []
    )


def test_unparseable_start_skipped():
    assert flights_from_schedule([_seg(start="whenever")]) == []


def test_missing_uid_skipped():
    assert flights_from_schedule([_seg(uid=None)]) == []


def test_none_schedule():
    assert flights_from_schedule(None) == []
