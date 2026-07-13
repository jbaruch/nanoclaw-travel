"""Tests for the live precheck core (build_plan).

Deterministic fixtures only — hand-built byAir records, pre-built meeting blocks,
a fake airport resolver and router, fixed `now`. These pin: airport + meeting
blocks are combined into ONE plan; legacy drive-planner (dp) blocks on the calendar
are LEFT UNTOUCHED (managed_legacy empty — the operator cleans them up); an
unresolvable airport is skipped, not guessed. The main() I/O layer is the outer
process boundary and is not unit-tested here.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import GEN_LEGACY_DP, ParsedBlock  # noqa: E402
from flight_identity import TRIPIT, Flight  # noqa: E402
from reconcile import DesiredBlock  # noqa: E402
from reconcile_sweep import ResolvedAirport, build_plan  # noqa: E402

UTC = timezone.utc
HOME = "12 Example St, TN"
NOW = datetime(2020, 7, 10, 12, 0, tzinfo=UTC)
US = "🇺🇸"

_IATA = {3: "JFK", 4: "BNA"}


def _resolve_airport(airport_id):
    iata = _IATA.get(airport_id)
    return None if iata is None else ResolvedAirport(iata=iata, flag=US, delay_index="low")


def _route(_o, _d):
    return timedelta(minutes=30)


def _record(fid, dep_id, arr_id, dep, arr):
    return {
        "schema_version": 6,
        "flight_id": fid,
        "code": "AA1",
        "trip_id": 7,
        "scheduled_dep_time": dep,
        "scheduled_arr_time": arr,
        "dep_airport_id": dep_id,
        "arr_airport_id": arr_id,
        "last_snapshot": None,
    }


def _meeting_block(identity="mtg1"):
    a = datetime(2020, 7, 12, 15, tzinfo=UTC)
    return DesiredBlock(
        identity=identity,
        kind="meeting_outbound",
        summary="Drive: Offsite",
        start=a - timedelta(minutes=30),
        end=a,
        origin="Home",
        destination="Venue",
        baseline_seconds=1800,
        anchor=a,
        timezone="America/Chicago",
    )


def _tripit_flight(dep, arr, sdep, sarr, *, seg="seg-1"):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=datetime.fromisoformat(sdep),
        scheduled_arr=datetime.fromisoformat(sarr),
        code="AA1",
        source=TRIPIT,
        tripit_segment_id=seg,
        trip_id=7,
    )


def test_tripit_only_flight_is_unioned_and_produces_legs():
    # A flight byAir never tracked (no records) still yields airport legs via the
    # TripIt union (R2). Its airports have no byAir facts — degraded but present.
    tf = _tripit_flight("ATL", "SJO", "2020-07-12T09:00:00+00:00", "2020-07-12T14:30:00+00:00")
    result = build_plan(
        flight_records=[],
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
        tripit_flights=[tf],
    )
    kinds = sorted(c.desired.kind for c in result.plan.creates)
    assert "airport_departure" in kinds and "airport_arrival" in kinds


def test_boarding_present_gates_trivial_suppression():
    # A trivial airport drive is suppressed only when a boarding block exists (V3).
    records = [_record(1, 4, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]

    def trivial_route(_o, _d):
        return timedelta(minutes=5)  # <= trivial threshold

    with_boarding = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=trivial_route,
        now=NOW,
        home_address=HOME,
        boarding_present=lambda _f: True,
    )
    without_boarding = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=trivial_route,
        now=NOW,
        home_address=HOME,
        boarding_present=lambda _f: False,
    )
    dep_with = [
        c.desired.kind for c in with_boarding.plan.creates if c.desired.kind == "airport_departure"
    ]
    dep_without = [
        c.desired.kind
        for c in without_boarding.plan.creates
        if c.desired.kind == "airport_departure"
    ]
    assert dep_with == []  # boarding present → trivial departure suppressed
    assert dep_without == ["airport_departure"]  # no boarding block → kept


def test_combines_airport_and_meeting_blocks():
    records = [_record(1, 4, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[_meeting_block()],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    kinds = sorted(c.desired.kind for c in result.plan.creates)
    # a single BNA->JFK flight yields departure + arrival; plus the meeting
    assert "meeting_outbound" in kinds
    assert "airport_departure" in kinds and "airport_arrival" in kinds


def test_legacy_dp_blocks_left_untouched():
    # An existing drive-planner meeting block must NOT be deleted or converted —
    # the operator cleans those up; the engine only manages its own blocks.
    dp = ParsedBlock(
        generation=GEN_LEGACY_DP,
        event_id="dp-swim",
        legacy_id="mtg-swim",
        legacy_direction="outbound",
    )
    result = build_plan(
        flight_records=[],
        resolve_airport=_resolve_airport,
        meeting_blocks=[_meeting_block()],
        current_blocks=[dp],
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    assert all(d.event_id != "dp-swim" for d in result.plan.deletes)
    assert result.plan.deletes == ()  # nothing deleted
    assert any(c.desired.kind == "meeting_outbound" for c in result.plan.creates)


def test_unresolved_airport_skipped():
    records = [_record(1, 9, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    assert result.plan.is_noop
    assert any("unresolved airport" in s for s in result.skipped)
