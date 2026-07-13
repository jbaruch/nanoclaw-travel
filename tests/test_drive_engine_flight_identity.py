"""Tests for the canonical flight identity and byAir ∪ TripIt union.

Deterministic fixtures only — fixed tz-aware datetimes, hand-written source
records, no generated inputs and no wall-clock. These pin the #156 W3 / R2 / V2
contract: identity keys on (dep_airport, arr_airport, scheduled_dep ± tolerance)
and NEVER on the designator, so codeshares and byAir's dual ids collapse to one
flight while consecutive daily operations stay distinct and single-source flights
survive.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from flight_identity import (  # noqa: E402
    BYAIR,
    DEFAULT_IDENTITY_TOLERANCE,
    TRIPIT,
    Flight,
    merge_flights,
)

UTC = timezone.utc


def _dt(y, mo, d, h, mi=0, *, offset_hours=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(hours=offset_hours)))


def byair(fid, code, dep, arr, sched_dep, *, sched_arr=None, live_dep=None, trip_id=None):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=sched_dep,
        scheduled_arr=sched_arr,
        code=code,
        source=BYAIR,
        live_dep=live_dep,
        byair_flight_id=fid,
        trip_id=trip_id,
    )


def tripit(seg, code, dep, arr, sched_dep, *, sched_arr=None, trip_id=None):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=sched_dep,
        scheduled_arr=sched_arr,
        code=code,
        source=TRIPIT,
        tripit_segment_id=seg,
        trip_id=trip_id,
    )


# --- Flight construction / normalization -----------------------------------


def test_naive_scheduled_dep_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        Flight(
            dep_airport="STN",
            arr_airport="CPH",
            scheduled_dep=datetime(2020, 7, 12, 9, 0),  # naive
            source=BYAIR,
            byair_flight_id=1,
        )


def test_iata_uppercased_and_utc_normalized():
    f = byair(1, "FR7382", "stn", "cph", _dt(2020, 7, 12, 11, 50, offset_hours=2))
    assert f.dep_airport == "STN"
    assert f.arr_airport == "CPH"
    assert f.scheduled_dep == datetime(2020, 7, 12, 9, 50, tzinfo=UTC)


def test_byair_requires_flight_id():
    with pytest.raises(ValueError, match="byair_flight_id"):
        Flight(
            dep_airport="STN", arr_airport="CPH", scheduled_dep=_dt(2020, 7, 12, 9), source=BYAIR
        )


def test_tripit_requires_segment_id():
    with pytest.raises(ValueError, match="tripit_segment_id"):
        Flight(
            dep_airport="STN", arr_airport="CPH", scheduled_dep=_dt(2020, 7, 12, 9), source=TRIPIT
        )


# --- Codeshare / dual-id collapse (V2) -------------------------------------


def test_codeshare_different_designators_collapse_to_one():
    # The live defect: same physical STN->CPH leg, two byAir ids, two codes.
    flights = [
        byair(6277117, "FR7382", "STN", "CPH", _dt(2020, 7, 12, 9, 0, offset_hours=0)),
        byair(7166978, "MW7382", "STN", "CPH", _dt(2020, 7, 12, 9, 5, offset_hours=0)),
    ]
    merged = merge_flights(flights)
    assert len(merged) == 1
    assert merged[0].byair_flight_ids == frozenset({6277117, 7166978})
    # designator did not split them; a code is retained for display
    assert merged[0].code in {"FR7382", "MW7382"}


def test_midnight_boundary_same_flight_collapses():
    # TripIt: 23:50 local (+02:00) on the 12th == 21:50Z. byAir: 22:10Z on the 12th.
    # Different calendar dates in local terms, ~20 min apart in true UTC -> one flight.
    flights = [
        tripit("seg-A", "SK915", "CPH", "EWR", _dt(2020, 7, 12, 23, 50, offset_hours=2)),
        byair(3358446, "SK915", "CPH", "EWR", _dt(2020, 7, 12, 22, 10, offset_hours=0)),
    ]
    merged = merge_flights(flights)
    assert len(merged) == 1
    assert merged[0].has_byair and merged[0].has_tripit


# --- byAir wins on times ----------------------------------------------------


def test_byair_wins_on_times_when_both_sources_present():
    sched_byair = _dt(2020, 7, 12, 9, 0, offset_hours=0)
    live = _dt(2020, 7, 12, 9, 40, offset_hours=0)
    flights = [
        tripit("seg-1", "FR7382", "STN", "CPH", _dt(2020, 7, 12, 9, 10, offset_hours=0)),
        byair(6277117, "FR7382", "STN", "CPH", sched_byair, live_dep=live),
    ]
    merged = merge_flights(flights)
    assert len(merged) == 1
    m = merged[0]
    assert m.scheduled_dep == sched_byair.astimezone(UTC)  # byAir scheduled, not TripIt's
    assert m.live_dep == live.astimezone(UTC)
    assert m.effective_dep == live.astimezone(UTC)  # live overrides scheduled


# --- Union: single-source flights survive (R2) -----------------------------


def test_byair_only_flight_survives():
    merged = merge_flights([byair(1, "DL4908", "CPH", "JFK", _dt(2020, 7, 12, 12))])
    assert len(merged) == 1
    assert merged[0].has_byair and not merged[0].has_tripit


def test_tripit_only_flight_survives():
    merged = merge_flights([tripit("seg-x", "AA100", "JFK", "BNA", _dt(2020, 7, 12, 18))])
    assert len(merged) == 1
    assert merged[0].has_tripit and not merged[0].has_byair


# --- Consecutive daily operations stay distinct ----------------------------


def test_same_route_24h_apart_not_merged():
    flights = [
        byair(1, "FR7382", "STN", "CPH", _dt(2020, 7, 12, 9)),
        byair(2, "FR7382", "STN", "CPH", _dt(2020, 7, 13, 9)),
    ]
    merged = merge_flights(flights)
    assert len(merged) == 2


def test_different_routes_not_merged():
    flights = [
        byair(1, "FR7382", "STN", "CPH", _dt(2020, 7, 12, 9)),
        byair(2, "SK915", "CPH", "EWR", _dt(2020, 7, 12, 9)),
    ]
    merged = merge_flights(flights)
    assert len(merged) == 2


# --- Tolerance boundary + anti-transitive clustering -----------------------


def test_just_inside_tolerance_merges():
    base = _dt(2020, 7, 12, 9, 0)
    flights = [
        byair(1, "X1", "STN", "CPH", base),
        byair(2, "X2", "STN", "CPH", base + DEFAULT_IDENTITY_TOLERANCE - timedelta(minutes=1)),
    ]
    assert len(merge_flights(flights)) == 1


def test_just_outside_tolerance_splits():
    base = _dt(2020, 7, 12, 9, 0)
    flights = [
        byair(1, "X1", "STN", "CPH", base),
        byair(2, "X2", "STN", "CPH", base + DEFAULT_IDENTITY_TOLERANCE + timedelta(minutes=1)),
    ]
    assert len(merge_flights(flights)) == 2


def test_anchor_prevents_transitive_swallow():
    # Three flights each ~5h apart: 1-2 within tolerance, 2-3 within tolerance,
    # but 1-3 are 10h apart. Anchoring on the cluster's first instant must NOT let
    # a chain of sub-tolerance steps merge flights 10h apart into one.
    base = _dt(2020, 7, 12, 6, 0)
    flights = [
        byair(1, "A", "STN", "CPH", base),
        byair(2, "B", "STN", "CPH", base + timedelta(hours=5)),
        byair(3, "C", "STN", "CPH", base + timedelta(hours=10)),
    ]
    merged = merge_flights(flights)
    # 1 anchors a cluster; 2 is within 6h of 1 (joins); 3 is 10h from anchor 1
    # (new cluster). Result: {1,2} and {3}.
    assert len(merged) == 2
    assert merged[0].byair_flight_ids == frozenset({1, 2})
    assert merged[1].byair_flight_ids == frozenset({3})


def test_output_is_deterministically_ordered():
    flights = [
        byair(2, "B", "CPH", "JFK", _dt(2020, 7, 12, 12)),
        byair(1, "A", "STN", "CPH", _dt(2020, 7, 12, 9)),
    ]
    merged = merge_flights(flights)
    assert [m.dep_airport for m in merged] == ["CPH", "STN"]


def test_negative_tolerance_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        merge_flights([], tolerance=timedelta(seconds=-1))


def test_empty_input():
    assert merge_flights([]) == []
