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
    # Producer-shaped: exactly the fields refresh-travel-schedule.py emits at
    # schema_version 2, with a realistic multi-line DESCRIPTION (the route lives on
    # the `[Flight] <DEP> to <ARR>` line; the SUMMARY is not relied on for route).
    seg = {
        "schema_version": 2,
        "summary": "DL 4908",
        "start": "2020-07-12T09:00:00Z",
        "end": "2020-07-12T14:30:00Z",
        "location": "Hartsfield-Jackson Atlanta International Airport (ATL)",
        "type": "Flight",
        "uid": "item-1@tripit.com",
        "description": (
            "[Flight] ATL to SJO\\nConfirmation #ABC123\\nhttps://www.tripit.com/trip/show/id/12345"
        ),
    }
    seg.update(over)
    return seg


def test_parses_route_code_and_times():
    flights = flights_from_schedule([_seg()])
    assert len(flights) == 1
    f = flights[0]
    assert f.source == TRIPIT
    assert f.dep_airport == "ATL" and f.arr_airport == "SJO"  # route from DESCRIPTION
    assert f.code == "DL4908"  # designator from SUMMARY
    assert f.scheduled_dep == datetime(2020, 7, 12, 9, 0, tzinfo=UTC)
    assert f.scheduled_arr == datetime(2020, 7, 12, 14, 30, tzinfo=UTC)
    assert f.tripit_segment_id == "item-1@tripit.com"


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


def test_trip_id_groups_legs_and_is_negated():
    # Two segments of one TripIt trip (same trip URL) get the SAME trip_id, so the
    # chain planner groups them as a connection rather than independent flights.
    # The id is negated to avoid colliding with byAir's positive trip_ids.
    trip_url = "https://www.tripit.com/trip/show/id/98765"
    leg1 = _seg(
        uid="item-1@tripit.com",
        summary="AA1 STN to CPH",
        description=f"[Flight] STN to CPH {trip_url}",
        start="2020-07-12T09:00:00Z",
        end="2020-07-12T11:00:00Z",
    )
    leg2 = _seg(
        uid="item-2@tripit.com",
        summary="AA2 CPH to JFK",
        description=f"[Flight] CPH to JFK {trip_url}",
        start="2020-07-12T13:00:00Z",
        end="2020-07-12T20:00:00Z",
    )
    flights = flights_from_schedule([leg1, leg2])
    assert len(flights) == 2
    assert flights[0].trip_id == flights[1].trip_id == -98765


def test_no_trip_url_leaves_trip_id_none():
    flights = flights_from_schedule([_seg(description="[Flight] ATL to SJO (no trip url here)")])
    assert flights[0].trip_id is None
