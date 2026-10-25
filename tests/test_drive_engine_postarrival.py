"""Post-arrival meeting composition (#350).

Deterministic fixtures only — fixed past UTC instants, injected routes, no
wall-clock, network, profile, calendar or account state. These pin the
accepted issue shape: no drive starts inside a flight interval; an ongoing
reachable meeting replaces airport-to-lodging; lodging wins over the trip city.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import GEN_UNIFIED, ParsedBlock, canonical_flight_key  # noqa: E402
from engine import AirportInfo, build_reconcile_plan  # noqa: E402
from flight_identity import BYAIR, Flight, merge_flights  # noqa: E402
from flight_mask import is_flight_event, known_flight_codes  # noqa: E402
from postarrival import (  # noqa: E402
    FlightGuard,
    PostarrivalMeeting,
    compose_postarrival,
)
from reconcile import DesiredBlock, HeldLeg  # noqa: E402

UTC = UTC
HOME = "12 Example St, TN"
HOTEL = "Hotel Address, Denver"
CITY = "Denver, CO"
VENUE = "Conference Center, Denver"
VENUE_B = "Other Venue, Denver"
US = "🇺🇸"
NOW = datetime(2020, 7, 10, 12, 0, tzinfo=UTC)
DEP = datetime(2020, 7, 12, 17, 20, tzinfo=UTC)
ARR = datetime(2020, 7, 12, 22, 10, tzinfo=UTC)
READY = datetime(2020, 7, 12, 22, 30, tzinfo=UTC)
GUARD = datetime(2020, 7, 12, 16, 20, tzinfo=UTC)
MEETING_START = datetime(2020, 7, 12, 20, 30, tzinfo=UTC)
MEETING_END = datetime(2020, 7, 12, 23, 45, tzinfo=UTC)
AIRPORT_DRIVE = timedelta(minutes=37)
CITY_TO_VENUE = timedelta(minutes=19)
VENUE_TO_HOTEL = timedelta(minutes=24)


def _dt(h, mi=0, *, day=12):
    return datetime(2020, 7, day, h, mi, tzinfo=UTC)


def _flight():
    return Flight(
        dep_airport="BNA",
        arr_airport="DEN",
        scheduled_dep=DEP,
        scheduled_arr=ARR,
        source=BYAIR,
        byair_flight_id=1,
        code="WN1",
    )


def _info(*codes):
    return {c: AirportInfo(flag=US, delay_index="low", timezone="America/Denver") for c in codes}


def _schedule(*, lodging=True, location=CITY):
    records = [
        {
            "type": "Trip",
            "summary": "Denver",
            "start": "2020-07-12",
            "end": "2020-07-14",
            "location": location,
        },
        {
            "type": "Flight",
            "summary": "WN1 BNA to DEN",
            "start": "2020-07-12T17:20:00Z",
            "end": "2020-07-12T22:10:00Z",
        },
    ]
    if lodging:
        records.extend(
            [
                {
                    "type": "Lodging",
                    "summary": "Check-in: Denver hotel",
                    "start": "2020-07-12T15:00:00Z",
                    "end": "2020-07-12T16:00:00Z",
                    "location": HOTEL,
                },
                {
                    "type": "Lodging",
                    "summary": "Check-out: Denver hotel",
                    "start": "2020-07-14T15:00:00Z",
                    "end": "2020-07-14T16:00:00Z",
                    "location": HOTEL,
                },
            ]
        )
    return records


def _meeting(*, identity="mtg-1", start=MEETING_START, end=MEETING_END, location=VENUE):
    return PostarrivalMeeting(
        identity=identity,
        summary="Keynote",
        location=location,
        start=start,
        end=end,
        timezone="America/Denver",
    )


def _meeting_blocks(*, origin=CITY, dest_back=CITY, start=MEETING_START, end=MEETING_END):
    return [
        DesiredBlock(
            identity="mtg-1",
            kind="meeting_outbound",
            summary="Drive: Keynote",
            start=start - CITY_TO_VENUE,
            end=start,
            origin=origin,
            destination=VENUE,
            baseline_seconds=int(CITY_TO_VENUE.total_seconds()),
            anchor=start,
        ),
        DesiredBlock(
            identity="mtg-1",
            kind="meeting_return",
            summary="Drive: Keynote",
            start=end,
            end=end + VENUE_TO_HOTEL,
            origin=VENUE,
            destination=dest_back,
            baseline_seconds=int(VENUE_TO_HOTEL.total_seconds()),
            anchor=end,
        ),
    ]


def _route(table, default_minutes=30):
    def route(origin, dest):
        if (origin, dest) in table:
            value = table[(origin, dest)]
            return None if value is None else timedelta(minutes=value)
        return timedelta(minutes=default_minutes)

    return route


ISSUE_ROUTE = _route(
    {
        ("DEN airport", VENUE): 37,
        ("DEN airport", HOTEL): 37,
        (VENUE, HOTEL): 24,
        (CITY, VENUE): 19,
        (VENUE, CITY): 24,
        (HOME, "BNA airport"): 30,
        ("BNA airport", HOME): 30,
    }
)


def _plan(**kwargs):
    params = {
        "flights": [_flight()],
        "airport_info": _info("BNA", "DEN"),
        "current_blocks": [],
        "route": ISSUE_ROUTE,
        "schedule": _schedule(),
        "home_address": HOME,
        "home_airports": frozenset({"BNA"}),
        "now": NOW,
        "extra_desired": _meeting_blocks(),
        "postarrival_meetings": [_meeting()],
    }
    params.update(kwargs)
    return build_reconcile_plan(**params)


def _created(result):
    return [c.desired for c in result.plan.creates]


def _flight_id():
    return canonical_flight_key(merge_flights([_flight()])[0])


def _block(identity, kind, *, start, origin, destination):
    return DesiredBlock(
        identity=identity,
        kind=kind,
        summary=f"Drive: {identity}",
        start=start,
        end=start + timedelta(minutes=20),
        origin=origin,
        destination=destination,
        baseline_seconds=1200,
        anchor=start,
    )


# --- case 1: exact issue shape ------------------------------------------------


def test_ongoing_reachable_meeting_replaces_airport_to_lodging():
    result = _plan()
    created = _created(result)
    assert all(not (GUARD <= b.start < READY) for b in created)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.identity == _flight_id()
    assert arrival.start == READY
    assert arrival.origin == "DEN airport"
    assert arrival.destination == VENUE
    assert arrival.end == READY + AIRPORT_DRIVE
    assert not any(b.kind == "meeting_outbound" for b in created)
    assert not any(b.destination == HOTEL and b.kind == "airport_arrival" for b in created)
    ret = next(b for b in created if b.kind == "meeting_return")
    assert ret.start == MEETING_END
    assert ret.origin == VENUE
    assert ret.destination == HOTEL
    assert CITY not in (arrival.origin, arrival.destination, ret.origin, ret.destination)


# --- case 2: reachability boundary -------------------------------------------


def test_equal_ready_plus_drive_and_meeting_end_is_unreachable():
    end = READY + AIRPORT_DRIVE
    result = _plan(
        extra_desired=_meeting_blocks(end=end),
        postarrival_meetings=[_meeting(end=end)],
    )
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not any(b.kind in {"meeting_outbound", "meeting_return"} for b in created)


def test_one_second_before_meeting_end_substitutes():
    end = READY + AIRPORT_DRIVE + timedelta(seconds=1)
    result = _plan(
        extra_desired=_meeting_blocks(end=end),
        postarrival_meetings=[_meeting(end=end)],
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == VENUE
    assert any(b.kind == "meeting_return" for b in _created(result))


# --- case 3: ended before readiness ------------------------------------------


def test_meeting_ended_before_ready_gets_no_drive_and_no_hold():
    end = datetime(2020, 7, 12, 22, 0, tzinfo=UTC)
    held = [
        HeldLeg(
            identity="mtg-1",
            kind="meeting_outbound",
            summary="Drive: Keynote",
            when=MEETING_START,
            reason="stale airborne",
        )
    ]
    result = _plan(
        extra_desired=_meeting_blocks(end=end),
        extra_held=held,
        postarrival_meetings=[_meeting(end=end)],
    )
    created = _created(result)
    assert next(b for b in created if b.kind == "airport_arrival").destination == HOTEL
    assert not any(b.kind in {"meeting_outbound", "meeting_return"} for b in created)
    assert all(h.identity != "mtg-1" for h in result.held)


# --- case 4: lodging precedence ----------------------------------------------


def test_handoff_and_return_avoid_trip_city_when_lodging_exists():
    result = _plan()
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    ret = next(b for b in created if b.kind == "meeting_return")
    assert CITY not in (arrival.origin, arrival.destination, ret.origin, ret.destination)
    assert ret.destination == HOTEL


# --- case 5: no lodging keeps trip-location fallback -------------------------


def test_no_lodging_keeps_trip_location_on_return():
    result = _plan(schedule=_schedule(lodging=False))
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    ret = next(b for b in created if b.kind == "meeting_return")
    assert arrival.origin == "DEN airport"
    assert arrival.destination == VENUE
    assert ret.destination == CITY


# --- case 6: existing bad unified blocks -------------------------------------


def test_reconcile_updates_arrival_and_return_and_deletes_outbound():
    fid = _flight_id()
    current = [
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="out-1",
            identity="mtg-1",
            kind="meeting_outbound",
            origin=CITY,
            destination=VENUE,
            baseline_seconds=1140,
            anchor=MEETING_START,
        ),
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="arr-1",
            identity=fid,
            kind="airport_arrival",
            origin="DEN airport",
            destination=HOTEL,
            baseline_seconds=2220,
            anchor=READY,
        ),
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="ret-1",
            identity="mtg-1",
            kind="meeting_return",
            origin=VENUE,
            destination=CITY,
            baseline_seconds=1440,
            anchor=MEETING_END,
        ),
    ]
    result = _plan(current_blocks=current)
    assert {u.event_id for u in result.plan.updates} == {"arr-1", "ret-1"}
    updated = {u.event_id: u.desired for u in result.plan.updates}
    assert updated["arr-1"].destination == VENUE
    assert updated["ret-1"].destination == HOTEL
    assert any(d.event_id == "out-1" for d in result.plan.deletes)
    assert not any(c.desired.kind == "meeting_outbound" for c in result.plan.creates)


# --- case 7: route failure ---------------------------------------------------


def test_handoff_route_failure_keeps_lodging_and_drops_unsafe_meeting():
    held = [
        HeldLeg(
            identity="mtg-1",
            kind="meeting_outbound",
            summary="Drive: Keynote",
            when=MEETING_START,
            reason="route failed",
        )
    ]
    result = _plan(
        route=_route(
            {("DEN airport", VENUE): None, ("DEN airport", HOTEL): 37, (VENUE, HOTEL): 24}
        ),
        extra_held=held,
    )
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not any(b.kind in {"meeting_outbound", "meeting_return"} for b in created)
    assert all(h.identity != "mtg-1" for h in result.held)
    assert any("airport-to-venue route failed" in s for s in result.skipped)


# --- case 8: guard boundary --------------------------------------------------


def test_block_at_ready_at_is_kept_block_at_guard_start_is_dropped():
    guard = FlightGuard(
        identity="BNA-DEN-20200712T1720Z",
        guard_start=GUARD,
        ready_at=READY,
        airport_code="DEN",
        airport_point="DEN airport",
        airport_label="DEN airport",
        chain_closing=True,
    )
    departure = DesiredBlock(
        identity="dep",
        kind="airport_departure",
        summary="Drive: → BNA",
        start=GUARD - timedelta(minutes=30),
        end=GUARD,
        origin=HOME,
        destination="BNA airport",
        baseline_seconds=1800,
        anchor=GUARD,
    )
    at_ready = _block(
        "ok", "lodging_outbound", start=READY, origin="DEN airport", destination=HOTEL
    )
    at_guard = _block("bad-g", "meeting_outbound", start=GUARD, origin=CITY, destination=VENUE)
    before_ready = _block(
        "bad-r",
        "meeting_return",
        start=READY - timedelta(microseconds=1),
        origin=VENUE,
        destination=CITY,
    )
    desired, held, _diags = compose_postarrival(
        guards=[guard],
        meetings=[],
        desired=[departure, at_ready, at_guard, before_ready],
        held=[],
        lodging_lookup=lambda _at: HOTEL,
        fallback_lookup=lambda _at: CITY,
        route=lambda _o, _d: timedelta(minutes=20),
    )
    kinds = {(b.identity, b.kind) for b in desired}
    assert ("dep", "airport_departure") in kinds
    assert ("ok", "lodging_outbound") in kinds
    assert ("bad-g", "meeting_outbound") not in kinds
    assert ("bad-r", "meeting_return") not in kinds
    assert next(b for b in desired if b.kind == "airport_departure").end == GUARD


# --- case 9: R5 identity mask ------------------------------------------------


def test_overlapping_ground_meeting_is_not_masked_as_a_flight():
    known = known_flight_codes(["WN1"])
    assert not is_flight_event("Keynote rehearsal", known)
    assert is_flight_event("Flight to Denver (WN 1)", known)
    result = _plan()
    assert any(b.kind == "airport_arrival" and b.destination == VENUE for b in _created(result))


# --- case 10: interior airside connection ------------------------------------


def test_airside_connection_does_not_source_a_meeting_handoff():
    chain = [
        Flight(
            dep_airport="STN",
            arr_airport="CPH",
            scheduled_dep=_dt(9),
            scheduled_arr=_dt(11),
            source=BYAIR,
            byair_flight_id=1,
            trip_id=7,
        ),
        Flight(
            dep_airport="CPH",
            arr_airport="JFK",
            scheduled_dep=_dt(13),
            scheduled_arr=_dt(20),
            source=BYAIR,
            byair_flight_id=2,
            trip_id=7,
        ),
        Flight(
            dep_airport="JFK",
            arr_airport="BNA",
            scheduled_dep=_dt(22),
            scheduled_arr=_dt(23, 30),
            source=BYAIR,
            byair_flight_id=3,
            trip_id=7,
        ),
    ]
    cph_meeting = PostarrivalMeeting(
        identity="cph-1",
        summary="CPH meetup",
        location="CPH lounge",
        start=_dt(11),
        end=_dt(12),
        timezone="Europe/Copenhagen",
    )
    extra = [
        DesiredBlock(
            identity="cph-1",
            kind="meeting_outbound",
            summary="Drive: CPH meetup",
            start=_dt(10, 40),
            end=_dt(11),
            origin="Copenhagen, Denmark",
            destination="CPH lounge",
            baseline_seconds=1200,
            anchor=_dt(11),
        )
    ]
    result = build_reconcile_plan(
        flights=chain,
        airport_info=_info("STN", "CPH", "JFK", "BNA"),
        current_blocks=[],
        route=lambda _o, _d: timedelta(minutes=30),
        home_address=HOME,
        now=NOW,
        extra_desired=extra,
        postarrival_meetings=[cph_meeting],
    )
    created = _created(result)
    assert not any(b.origin == "CPH airport" and b.destination == "CPH lounge" for b in created)
    assert {b.kind for b in created} == {"airport_departure", "airport_arrival"}


# --- case 11: ambiguous simultaneous meetings --------------------------------


def test_two_overlapping_meetings_do_not_pick_by_input_order():
    second = _meeting(identity="mtg-2", location=VENUE_B)
    extra = _meeting_blocks() + [
        DesiredBlock(
            identity="mtg-2",
            kind="meeting_outbound",
            summary="Drive: Other",
            start=MEETING_START - CITY_TO_VENUE,
            end=MEETING_START,
            origin=CITY,
            destination=VENUE_B,
            baseline_seconds=1140,
            anchor=MEETING_START,
        ),
        DesiredBlock(
            identity="mtg-2",
            kind="meeting_return",
            summary="Drive: Other",
            start=MEETING_END,
            end=MEETING_END + VENUE_TO_HOTEL,
            origin=VENUE_B,
            destination=CITY,
            baseline_seconds=1440,
            anchor=MEETING_END,
        ),
    ]
    result = _plan(
        extra_desired=extra,
        postarrival_meetings=[_meeting(), second],
        route=_route(
            {
                ("DEN airport", VENUE): 37,
                ("DEN airport", VENUE_B): 37,
                ("DEN airport", HOTEL): 37,
                (VENUE, HOTEL): 24,
                (VENUE_B, HOTEL): 24,
            }
        ),
    )
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not any(b.kind in {"meeting_outbound", "meeting_return"} for b in created)
    assert any("overlapping meetings" in s for s in result.skipped)
