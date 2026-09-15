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

from block_codec import GEN_LEGACY_FADRIVE, GEN_UNIFIED, ParsedBlock  # noqa: E402
from engine import AirportInfo, build_reconcile_plan  # noqa: E402
from flight_identity import BYAIR, TRIPIT, Flight  # noqa: E402

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


def tripit_flight(dep, arr, sdep, sarr, *, segment_id, trip_id, code=None):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=sdep,
        scheduled_arr=sarr,
        code=code,
        source=TRIPIT,
        tripit_segment_id=segment_id,
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
    # airport endpoint is geocodable ("BNA airport"), not a bare IATA code (#165)
    assert dep.origin == HOME and dep.destination == "BNA airport"


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
    assert created == {("airport_departure", "STN airport"), ("airport_arrival", HOME)}
    assert len(result.plan.creates) == 2


def test_round_trip_closing_arrival_returns_home_on_trip_end_day():
    """Regression for the live BNA→San Francisco block on the return landing.

    The Trip wrapper and last lodging remain active through the return date. The
    chain still began from home and closes at its opening airport, so its final
    airport-arrival drive must end at home, not at the trip destination.
    """
    # Exact live grouping shape: byAir split the outbound and return into two
    # trip ids, while TripIt linked both physical-flight twins under one shared
    # itinerary id. The merged aliases must reconnect the round trip.
    chain = [
        flight("BNA", "SFO", _dt(9, day=12), _dt(14, day=12), fid=1, trip_id=2832675),
        tripit_flight(
            "BNA",
            "SFO",
            _dt(9, day=12),
            _dt(14, day=12),
            segment_id="outbound",
            trip_id=-384031073,
        ),
        flight("SFO", "BNA", _dt(18, day=14), _dt(22, day=14), fid=2, trip_id=2832677),
        tripit_flight(
            "SFO",
            "BNA",
            _dt(18, day=14),
            _dt(22, day=14),
            segment_id="return",
            trip_id=-384031073,
        ),
    ]
    schedule = [
        {
            "type": "Trip",
            "summary": "San Francisco",
            "start": "2020-07-12",
            "end": "2020-07-14",
            "location": "San Francisco, CA",
        },
        {
            "type": "Flight",
            "summary": "BNA to SFO",
            "start": "2020-07-12T09:00:00Z",
            "end": "2020-07-12T14:00:00Z",
        },
        {
            "type": "Lodging",
            "summary": "San Francisco hotel",
            "start": "2020-07-12T16:00:00Z",
            "end": "2020-07-14T15:00:00Z",
            "location": "1 Market St, San Francisco, CA",
        },
        {
            "type": "Flight",
            "summary": "SFO to BNA",
            "start": "2020-07-14T18:00:00Z",
            "end": "2020-07-14T22:00:00Z",
        },
    ]
    result = build_reconcile_plan(
        flights=chain,
        airport_info=_us_info("BNA", "SFO"),
        current_blocks=[],
        route=const_route(30),
        schedule=schedule,
        home_address=HOME,
        now=NOW,
    )

    arrivals = [
        create.desired for create in result.plan.creates if create.desired.kind == "airport_arrival"
    ]
    assert [arrival.destination for arrival in arrivals] == [
        "San Francisco, CA",
        HOME,
    ]
    assert arrivals[-1].summary == "Drive: BNA → home"

    stale = ParsedBlock(
        generation=GEN_UNIFIED,
        event_id="bad-return",
        identity=arrivals[-1].identity,
        kind=arrivals[-1].kind,
        baseline_seconds=120_000,
        anchor=arrivals[-1].anchor,
        origin="BNA airport",
        destination="San Francisco, CA",
    )
    repair = build_reconcile_plan(
        flights=chain,
        airport_info=_us_info("BNA", "SFO"),
        current_blocks=[stale],
        route=const_route(30),
        schedule=schedule,
        home_address=HOME,
        now=NOW,
    )
    assert len(repair.plan.updates) == 1
    assert repair.plan.updates[0].event_id == "bad-return"
    assert repair.plan.updates[0].desired.destination == HOME


def test_shared_alias_preserves_long_stay_ground_legs_without_lodging():
    """Live Oslo follow-up: a shared TripIt itinerary identifies homecoming,
    but must not turn the multi-day stay into an airside connection."""
    flights = [
        flight("BNA", "OSL", _dt(9, day=12), _dt(14, day=12), fid=1, trip_id=101),
        tripit_flight(
            "BNA",
            "OSL",
            _dt(9, day=12),
            _dt(14, day=12),
            segment_id="outbound",
            trip_id=-900,
        ),
        flight("OSL", "BNA", _dt(18, day=14), _dt(22, day=14), fid=2, trip_id=202),
        tripit_flight(
            "OSL",
            "BNA",
            _dt(18, day=14),
            _dt(22, day=14),
            segment_id="return",
            trip_id=-900,
        ),
    ]
    schedule = [
        {
            "type": "Trip",
            "summary": "Oslo",
            "start": "2020-07-12",
            "end": "2020-07-14",
            "location": "Oslo, Norway",
        },
        {
            "type": "Flight",
            "summary": "BNA to OSL",
            "start": "2020-07-12T09:00:00Z",
            "end": "2020-07-12T14:00:00Z",
        },
        {
            "type": "Flight",
            "summary": "OSL to BNA",
            "start": "2020-07-14T18:00:00Z",
            "end": "2020-07-14T22:00:00Z",
        },
    ]

    result = build_reconcile_plan(
        flights=flights,
        airport_info=_us_info("BNA", "OSL"),
        current_blocks=[],
        route=const_route(30),
        schedule=schedule,
        home_address=HOME,
        now=NOW,
    )

    created = [create.desired for create in result.plan.creates]
    arrivals = [block for block in created if block.kind == "airport_arrival"]
    departures = [block for block in created if block.kind == "airport_departure"]
    assert [(block.summary, block.destination) for block in arrivals] == [
        ("Drive: OSL → Oslo, Norway", "Oslo, Norway"),
        ("Drive: BNA → home", HOME),
    ]
    assert {block.destination for block in departures} == {"BNA airport", "OSL airport"}
    assert all(block.kind != "airport_transfer" for block in created)


def test_in_trip_same_airport_loop_returns_to_lodging_not_static_home():
    """A loop that starts while already away closes at the trip lodging."""
    chain = [
        flight("OSL", "BGO", _dt(9, day=13), _dt(10, day=13), fid=1, trip_id=8),
        flight("BGO", "OSL", _dt(18, day=13), _dt(19, day=13), fid=2, trip_id=8),
    ]
    schedule = [
        {
            "type": "Trip",
            "summary": "Oslo",
            "start": "2020-07-11",
            "end": "2020-07-15",
            "location": "Oslo, Norway",
        },
        {
            "type": "Flight",
            "summary": "BNA to OSL",
            "start": "2020-07-11T09:00:00Z",
            "end": "2020-07-11T18:00:00Z",
        },
        {
            "type": "Lodging",
            "summary": "Oslo hotel",
            "start": "2020-07-11T20:00:00Z",
            "end": "2020-07-15T10:00:00Z",
            "location": "Oslo hotel address",
        },
    ]
    result = build_reconcile_plan(
        flights=chain,
        airport_info=_us_info("OSL", "BGO"),
        current_blocks=[],
        route=const_route(30),
        schedule=schedule,
        home_address=HOME,
        now=NOW,
    )

    closing = [
        create.desired for create in result.plan.creates if create.desired.kind == "airport_arrival"
    ][-1]
    assert closing.destination == "Oslo hotel address"


def test_past_flight_builds_no_legs_and_is_skipped():
    """#165: a flight whose drive windows are already in the past (a completed
    trip — the operator is home again) produces NO airport legs. It's skipped
    cleanly, never routed from the current home to a faraway, already-flown
    airport (which has no drive route and just fails with noise). `const_route`
    would happily route it, so this pins the past-filter, not a route failure."""
    f = flight("BNA", "JFK", _dt(9, day=8), _dt(11, day=8), fid=1)  # 2 days before NOW
    result = build_reconcile_plan(
        flights=[f],
        airport_info=_us_info("BNA", "JFK"),
        current_blocks=[],
        route=const_route(30),
        home_address=HOME,
        now=NOW,
    )
    assert result.plan.creates == ()
    assert any("past" in s for s in result.skipped)


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


# ---------------------------------------------------------------------------
# Homecoming survives an airport hotel the night before (#235)
# ---------------------------------------------------------------------------


def _staged_round_trip_schedule():
    """BNA→SFO→BNA with an airport hotel the operator drives to the night before.

    He is at a hotel when the outbound drive leaves, but he got there from the
    house — so the trip still closes at the house.
    """
    return [
        {
            "type": "Trip",
            "summary": "San Francisco",
            "start": "2020-07-11",
            "end": "2020-07-14",
            "location": "San Francisco, CA",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hyatt Place Nashville Airport",
            "start": "2020-07-11T22:00:00Z",
            "end": "2020-07-11T23:00:00Z",
            "location": "Hyatt Place Nashville Airport",
        },
        {
            "type": "Flight",
            "summary": "BNA to SFO",
            "start": "2020-07-12T09:00:00Z",
            "end": "2020-07-12T14:00:00Z",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: San Francisco hotel",
            "start": "2020-07-12T16:00:00Z",
            "end": "2020-07-14T15:00:00Z",
            "location": "1 Market St, San Francisco, CA",
        },
        {
            "type": "Flight",
            "summary": "SFO to BNA",
            "start": "2020-07-14T18:00:00Z",
            "end": "2020-07-14T22:00:00Z",
        },
    ]


def _staged_round_trip_chain():
    return [
        flight("BNA", "SFO", _dt(9), _dt(14), fid=1, trip_id=2832676),
        flight("SFO", "BNA", _dt(18, day=14), _dt(22, day=14), fid=2, trip_id=2832676),
    ]


def test_a_staged_round_trip_still_drives_home_at_the_end():
    """The homecoming test used to read the operator's position at the outbound
    drive and see `lodging`, so the final arrival routed to the airport hotel
    instead of the house. It now asks whether the JOURNEY left home (#235)."""
    result = build_reconcile_plan(
        flights=_staged_round_trip_chain(),
        airport_info=_us_info("BNA", "SFO"),
        current_blocks=[],
        route=const_route(30),
        schedule=_staged_round_trip_schedule(),
        home_address=HOME,
        now=NOW,
    )
    arrivals = [c.desired for c in result.plan.creates if c.desired.kind == "airport_arrival"]
    assert arrivals[-1].destination == HOME
    assert arrivals[-1].summary == "Drive: BNA → home"


def test_the_staged_outbound_drive_starts_at_the_hotel_not_the_house():
    """The other half of the same fix: he slept at the hotel, so that is where
    the airport drive begins."""
    result = build_reconcile_plan(
        flights=_staged_round_trip_chain(),
        airport_info=_us_info("BNA", "SFO"),
        current_blocks=[],
        route=const_route(30),
        schedule=_staged_round_trip_schedule(),
        home_address=HOME,
        now=NOW,
    )
    departures = [c.desired for c in result.plan.creates if c.desired.kind == "airport_departure"]
    assert departures[0].origin == "Hyatt Place Nashville Airport"


def test_a_trip_driven_to_then_flown_out_of_does_not_drive_home_to_tennessee():
    """The outcome the unit test implies: he drove to Gatlinburg days earlier,
    so the local round trip lands back at the destination hotel. Routing that
    final drive to the house would be a cross-state block (#235 review)."""
    schedule = [
        {
            "type": "Trip",
            "summary": "Gatlinburg",
            "start": "2020-07-08",
            "end": "2020-07-16",
            "location": "Gatlinburg, TN",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Fairfield Inn",
            "start": "2020-07-08T20:00:00Z",
            "end": "2020-07-08T21:00:00Z",
            "location": "611 Historic Nature Trail Gatlinburg TN",
        },
        {
            "type": "Flight",
            "summary": "BNA to SFO",
            "start": "2020-07-12T09:00:00Z",
            "end": "2020-07-12T14:00:00Z",
        },
        {
            "type": "Flight",
            "summary": "SFO to BNA",
            "start": "2020-07-14T18:00:00Z",
            "end": "2020-07-14T22:00:00Z",
        },
    ]
    result = build_reconcile_plan(
        flights=[
            flight("BNA", "SFO", _dt(9), _dt(14), fid=1, trip_id=901),
            flight("SFO", "BNA", _dt(18, day=14), _dt(22, day=14), fid=2, trip_id=901),
        ],
        airport_info=_us_info("BNA", "SFO"),
        current_blocks=[],
        route=const_route(30),
        schedule=schedule,
        home_address=HOME,
        now=NOW,
    )
    arrivals = [c.desired for c in result.plan.creates if c.desired.kind == "airport_arrival"]
    assert arrivals[-1].destination != HOME
    assert "Gatlinburg" in arrivals[-1].destination


def test_airport_arrival_on_home_metro_placeholder_returns_home():
    f = flight("JFK", "BNA", _dt(9), _dt(11), fid=1)
    result = build_reconcile_plan(
        flights=[f],
        airport_info=_us_info("JFK", "BNA"),
        current_blocks=[],
        route=const_route(40),
        home_address=HOME,
        home_metros=frozenset({"nashville, tn"}),
        now=NOW,
        schedule=[
            {
                "type": "Trip",
                "summary": "Local placeholder",
                "start": "2020-07-12",
                "end": "2020-07-12",
                "location": "Nashville, TN",
            }
        ],
    )
    arrival = next(c.desired for c in result.plan.creates if c.desired.kind == "airport_arrival")
    assert arrival.destination == HOME
    assert arrival.baseline_seconds == 2400
