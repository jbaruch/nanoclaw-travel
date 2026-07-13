"""End-to-end tests for the engine orchestration (all pure modules composed).

Deterministic fixtures only — hand-built flights, a fake router, fixed reference
`now`, no wall-clock. These drive the whole pipeline (normalize-shaped Flights →
merge → chains → classify → anchor → route → desired → reconcile) with injected
dependencies, and pin the behaviors that only emerge from composition: a single
flight yields a home→airport and airport→home block; a multi-leg same-airport
connection chain yields only the ground-endpoint legs; the live 2020-07-12
itinerary converges its legacy storm and deletes the connection orphans; trivial
and unroutable legs are suppressed/skipped.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import GEN_LEGACY_FADRIVE, ParsedBlock  # noqa: E402
from engine import AirportInfo, build_reconcile_plan  # noqa: E402
from flight_identity import BYAIR, Flight  # noqa: E402

UTC = timezone.utc
HOME = "12 Example St, TN"
NOW = datetime(2020, 7, 10, 12, 0, tzinfo=UTC)  # well before the trip → plan wins
US = "🇺🇸"


def _dt(h, mi=0, *, day=12):
    return datetime(2020, 7, day, h, mi, tzinfo=UTC)


def flight(dep, arr, sdep, sarr, *, fid, trip_id=None, code=None):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=sdep,
        scheduled_arr=sarr,
        code=code,
        source=BYAIR,
        byair_flight_id=fid,
        trip_id=trip_id,
    )


def _us_info(*codes):
    return {c: AirportInfo(flag=US, delay_index="low", timezone="America/Chicago") for c in codes}


def test_airport_blocks_carry_airport_timezone():
    f = flight("BNA", "JFK", _dt(9), _dt(11), fid=1)
    result = build_reconcile_plan(
        flights=[f],
        airport_info=_us_info("BNA", "JFK"),
        current_blocks=[],
        route=const_route(30),
        home_address=HOME,
        now=NOW,
    )
    for c in result.plan.creates:
        assert c.desired.timezone == "America/Chicago"  # not None / UTC-defaulted


def const_route(minutes):
    return lambda o, d: timedelta(minutes=minutes)


def legacy(flight_id, direction, event_id):
    return ParsedBlock(
        generation=GEN_LEGACY_FADRIVE,
        event_id=event_id,
        legacy_id=flight_id,
        legacy_direction=direction,
    )


# --- single flight ----------------------------------------------------------


def test_single_domestic_flight_creates_departure_and_arrival():
    f = flight("BNA", "JFK", _dt(9), _dt(11), fid=1)
    result = build_reconcile_plan(
        flights=[f],
        airport_info=_us_info("BNA", "JFK"),
        current_blocks=[],
        route=const_route(30),
        home_address=HOME,
        now=NOW,
    )
    kinds = sorted(c.desired.kind for c in result.plan.creates)
    assert kinds == ["airport_arrival", "airport_departure"]
    dep = next(c.desired for c in result.plan.creates if c.desired.kind == "airport_departure")
    # anchor = 09:00 − 60 clearance = 08:00; leave_by = 08:00 − 30 drive = 07:30
    assert dep.anchor == _dt(8)
    assert dep.start == _dt(7, 30)
    assert dep.origin == HOME and dep.destination == "BNA"


# --- same-airport connection chain -----------------------------------------


def test_connection_chain_yields_only_ground_endpoints():
    # STN→CPH→JFK→BNA, one trip, same-airport connections at CPH and JFK.
    chain = [
        flight("STN", "CPH", _dt(9), _dt(11), fid=1, trip_id=7),
        flight("CPH", "JFK", _dt(13), _dt(20), fid=2, trip_id=7),
        flight("JFK", "BNA", _dt(22), _dt(23, 30), fid=3, trip_id=7),
    ]
    result = build_reconcile_plan(
        flights=chain,
        airport_info=_us_info("STN", "CPH", "JFK", "BNA"),
        current_blocks=[],
        route=const_route(30),
        home_address=HOME,
        now=NOW,
    )
    created = {(c.desired.kind, c.desired.destination) for c in result.plan.creates}
    # exactly the opening departure (to STN) and the closing arrival (from BNA)
    assert created == {("airport_departure", "STN"), ("airport_arrival", HOME)}
    assert len(result.plan.creates) == 2


def test_jul12_itinerary_converges_storm_and_deletes_connection_orphans():
    chain = [
        flight("STN", "CPH", _dt(9), _dt(11), fid=6277117, trip_id=7),
        flight("CPH", "JFK", _dt(13), _dt(20), fid=3358446, trip_id=7),
        flight("JFK", "BNA", _dt(22), _dt(23, 30), fid=3359520, trip_id=7),
    ]
    current = (
        [legacy("6277117", "to_airport", f"stn{i}") for i in range(5)]
        + [legacy("3358446", "to_airport", f"cph{i}") for i in range(7)]
        + [legacy("3359520", "to_airport", "jfk1")]
    )
    result = build_reconcile_plan(
        flights=chain,
        airport_info=_us_info("STN", "CPH", "JFK", "BNA"),
        current_blocks=current,
        route=const_route(30),
        home_address=HOME,
        now=NOW,
    )
    plan = result.plan
    # STN departure converges the 5 legacy STN blocks
    assert len(plan.converts) == 1
    assert len(plan.converts[0].legacy_event_ids) == 5
    # the closing BNA arrival is a fresh create (no legacy for it)
    assert any(c.desired.kind == "airport_arrival" for c in plan.creates)
    # the 7 CPH + 1 JFK connection blocks are orphan-deleted
    assert len(plan.deletes) == 8
    assert all("legacy orphan" in d.reason for d in plan.deletes)


# --- suppression / degrade --------------------------------------------------


def test_trivial_departure_suppressed_when_boarding_present():
    f = flight("BNA", "JFK", _dt(9), _dt(11), fid=1)
    result = build_reconcile_plan(
        flights=[f],
        airport_info=_us_info("BNA", "JFK"),
        current_blocks=[],
        route=const_route(5),  # ≤ trivial threshold
        home_address=HOME,
        now=NOW,
        boarding_present=lambda _f: True,
    )
    kinds = [c.desired.kind for c in result.plan.creates]
    assert "airport_departure" not in kinds  # suppressed
    assert any("trivial" in s for s in result.skipped)


def test_trivial_not_suppressed_without_boarding_block():
    f = flight("BNA", "JFK", _dt(9), _dt(11), fid=1)
    result = build_reconcile_plan(
        flights=[f],
        airport_info=_us_info("BNA", "JFK"),
        current_blocks=[],
        route=const_route(5),
        home_address=HOME,
        now=NOW,
        boarding_present=lambda _f: False,  # no presence block → keep the drive
    )
    kinds = [c.desired.kind for c in result.plan.creates]
    assert "airport_departure" in kinds


def test_unroutable_leg_is_skipped_with_diagnostic():
    f = flight("BNA", "JFK", _dt(9), _dt(11), fid=1)
    result = build_reconcile_plan(
        flights=[f],
        airport_info=_us_info("BNA", "JFK"),
        current_blocks=[],
        route=lambda o, d: None,  # every route fails
        home_address=HOME,
        now=NOW,
    )
    assert result.plan.creates == ()
    assert len(result.skipped) >= 1


def test_no_home_off_trip_skips_rather_than_routing_blind():
    f = flight("BNA", "JFK", _dt(9), _dt(11), fid=1)
    result = build_reconcile_plan(
        flights=[f],
        airport_info=_us_info("BNA", "JFK"),
        current_blocks=[],
        route=const_route(30),
        home_address=None,  # off-trip, no home → position_at unresolved
        now=NOW,
    )
    assert result.plan.creates == ()
    assert any("unresolved" in s for s in result.skipped)
