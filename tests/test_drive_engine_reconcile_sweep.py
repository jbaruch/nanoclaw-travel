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
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

import pytest  # noqa: E402
from block_codec import GEN_LEGACY_DP, ParsedBlock  # noqa: E402
from engine import PlanBudgetExceeded  # noqa: E402
from flight_identity import TRIPIT, Flight  # noqa: E402
from maps_client import MapsError, TravelTime  # noqa: E402
from reconcile import DesiredBlock  # noqa: E402
from reconcile_sweep import ResolvedAirport, build_plan, make_route  # noqa: E402

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


def _tripit_flight(dep, arr, sdep, sarr, *, seg="seg-1", trip_id=7):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=datetime.fromisoformat(sdep),
        scheduled_arr=datetime.fromisoformat(sarr),
        code="AA1",
        source=TRIPIT,
        tripit_segment_id=seg,
        trip_id=trip_id,
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


def test_tripit_only_connection_groups_no_interior_legs():
    # Two TripIt-only legs of ONE trip (shared trip_id) with a same-airport
    # connection (CPH) must be recognized as a connection — only the opening
    # departure (STN) and closing arrival (JFK), NOT independent per-leg drives.
    legs = [
        _tripit_flight(
            "STN",
            "CPH",
            "2020-07-12T09:00:00+00:00",
            "2020-07-12T11:00:00+00:00",
            seg="s1",
            trip_id=-98765,
        ),
        _tripit_flight(
            "CPH",
            "JFK",
            "2020-07-12T13:00:00+00:00",
            "2020-07-12T20:00:00+00:00",
            seg="s2",
            trip_id=-98765,
        ),
    ]
    result = build_plan(
        flight_records=[],
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
        tripit_flights=legs,
    )
    created = {(c.desired.kind, c.desired.destination) for c in result.plan.creates}
    assert created == {("airport_departure", "STN airport"), ("airport_arrival", HOME)}


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


def test_no_home_off_trip_degrades_not_crashes():
    # With no home_address and no active trip, the airport side must degrade (legs
    # skipped with a diagnostic) rather than raise — a missing home never takes the
    # sweep down (#162). The meeting side is guarded separately in main().
    records = [_record(1, 4, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=None,  # neither config nor user_profile provided one
    )
    assert result.plan.creates == ()  # no origin → no blind block
    assert any("unresolved" in s for s in result.skipped)


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


# --- make_route memoization (#172) ------------------------------------------


class _FakeMaps:
    """Counts travel_time calls and can be told to fail, to pin memoization."""

    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    def travel_time(self, origin: str, destination: str) -> TravelTime:
        self.calls.append((origin, destination))
        if self._fail:
            raise MapsError("ALL_PROVIDERS_FAILED", "boom")
        return TravelTime(
            duration_seconds=1800,
            in_traffic_seconds=1800,
            traffic_factor=1.0,
            distance_meters=1000,
            origin_resolved=origin,
            destination_resolved=destination,
            source="google",
        )


def test_make_route_memoizes_repeated_pair():
    """A repeated (origin, destination) pair — an airport that is both a departure
    destination and a transfer origin — routes ONCE, not per leg (#172)."""
    maps = _FakeMaps()
    route = make_route(maps)
    first = route("home", "STN airport")
    second = route("home", "STN airport")
    assert first == second == timedelta(seconds=1800)
    assert maps.calls == [("home", "STN airport")]  # one round trip, not two


def test_make_route_distinct_pairs_each_route_once():
    maps = _FakeMaps()
    route = make_route(maps)
    route("home", "STN airport")
    route("STN airport", "CPH airport")
    route("home", "STN airport")  # repeat of the first
    assert maps.calls == [("home", "STN airport"), ("STN airport", "CPH airport")]


def test_make_route_caches_failure_as_none():
    """A dead endpoint caches None so it isn't re-attempted every leg (each retry
    is the same slow provider-failover that caused the storm) (#172)."""
    maps = _FakeMaps(fail=True)
    route = make_route(maps)
    assert route("home", "STN airport") is None
    assert route("home", "STN airport") is None
    assert maps.calls == [("home", "STN airport")]  # failure not re-attempted


def test_make_route_raises_past_deadline_before_network_call():
    """#172: past the budget deadline a cache MISS raises rather than entering the
    provider-fallback chain — so a slow leg can't push the sweep past its budget
    after the per-leg poll already passed. No network call is made."""
    maps = _FakeMaps()
    route = make_route(maps, deadline=100.0, clock=lambda: 100.0)
    with pytest.raises(PlanBudgetExceeded):
        route("home", "STN airport")
    assert maps.calls == []  # aborted before the travel_time call


def test_make_route_serves_cache_hit_even_past_deadline():
    """A cached pair is free, so it's served even past the deadline — only a MISS
    (a new network call) is gated (#172)."""
    cache: dict[tuple[str, str], timedelta | None] = {}
    now = {"t": 0.0}
    route = make_route(maps=_FakeMaps(), cache=cache, deadline=100.0, clock=lambda: now["t"])
    before = route("home", "STN airport")  # populates the cache before the deadline
    now["t"] = 200.0  # now well past the deadline
    after = route("home", "STN airport")  # cache hit — must not raise
    assert before == after == timedelta(seconds=1800)


def test_make_route_next_miss_raises_after_a_slow_call_crosses_deadline():
    """#172: the 'last route succeeds after the deadline' case. A miss that starts
    under the deadline is allowed to finish (even though the clock then crosses the
    deadline), but the NEXT miss raises — no further provider-fallback chain is
    entered, so the sweep reaches its clean no-wake path deterministically."""
    maps = _FakeMaps()
    now = {"t": 14.0}  # under the 15.0 deadline

    def clock() -> float:
        return now["t"]

    route = make_route(maps, deadline=15.0, clock=clock)
    first = route("home", "STN airport")  # begins under budget, completes
    assert first == timedelta(seconds=1800)
    now["t"] = 31.0  # that call returned well past the deadline
    with pytest.raises(PlanBudgetExceeded):
        route("STN airport", "CPH airport")  # a new miss — refused
    assert maps.calls == [("home", "STN airport")]  # the second never hit the network
