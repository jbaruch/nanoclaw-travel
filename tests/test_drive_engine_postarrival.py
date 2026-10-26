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
from itinerary import Itinerary  # noqa: E402
from meeting_source import meeting_desired_blocks  # noqa: E402
from postarrival import (  # noqa: E402
    FlightGuard,
    PostarrivalMeeting,
    compose_postarrival,
    postarrival_meetings_from,
)
from reconcile import DesiredBlock, HeldLeg  # noqa: E402
from reconcile_sweep import meeting_anchor  # noqa: E402
from scan import scan  # noqa: E402

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


def _meeting_suppressed():
    return PostarrivalMeeting(
        identity="mtg-1",
        summary="Keynote",
        location=VENUE,
        start=MEETING_START,
        end=MEETING_END,
        timezone="America/Denver",
        return_suppressed=True,
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


def _route(table, default_minutes: int | None = 30):
    def route(origin, dest):
        if (origin, dest) in table:
            value = table[(origin, dest)]
            return None if value is None else timedelta(minutes=value)
        if default_minutes is None:
            return None
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


# --- case 7b: return-route failure is a proposed-handoff failure (B2) --------


def test_return_route_failure_keeps_lodging_and_drops_unsafe_meeting():
    result = _plan(
        route=_route(
            {("DEN airport", VENUE): 37, ("DEN airport", HOTEL): 37, (VENUE, HOTEL): None}
        ),
    )
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not any(b.kind in {"meeting_outbound", "meeting_return"} for b in created)
    assert all(h.identity != "mtg-1" for h in result.held)
    assert any("venue-to-lodging route failed" in s for s in result.skipped)


# --- F2 injected: suppressed return keeps the arrival handoff ----------------


def test_suppressed_return_keeps_handoff_and_omits_hotel_return():
    result = _plan(postarrival_meetings=[_meeting_suppressed()])
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == VENUE
    assert not any(b.kind == "meeting_return" for b in created)
    assert not any(b.kind == "meeting_outbound" for b in created)


# --- F3 pairing on desired blocks -------------------------------------------


def test_protected_return_keeps_unprotected_outbound():
    extra = [
        DesiredBlock(
            identity="pre-1",
            kind="meeting_outbound",
            summary="Drive: Office",
            start=datetime(2020, 7, 12, 13, 30, tzinfo=UTC),
            end=datetime(2020, 7, 12, 14, 0, tzinfo=UTC),
            origin=HOME,
            destination="Nashville Office",
            baseline_seconds=1800,
            anchor=datetime(2020, 7, 12, 14, 0, tzinfo=UTC),
        ),
        DesiredBlock(
            identity="pre-1",
            kind="meeting_return",
            summary="Drive: Office",
            start=datetime(2020, 7, 12, 16, 30, tzinfo=UTC),
            end=datetime(2020, 7, 12, 17, 0, tzinfo=UTC),
            origin="Nashville Office",
            destination=HOME,
            baseline_seconds=1800,
            anchor=datetime(2020, 7, 12, 16, 30, tzinfo=UTC),
        ),
    ]
    result = _plan(extra_desired=extra, postarrival_meetings=[])
    created = _created(result)
    assert any(b.identity == "pre-1" and b.kind == "meeting_outbound" for b in created)
    assert not any(b.identity == "pre-1" and b.kind == "meeting_return" for b in created)


def test_protected_outbound_drops_both_meeting_keys():
    extra = [
        DesiredBlock(
            identity="pre-2",
            kind="meeting_outbound",
            summary="Drive: Late office",
            start=GUARD,
            end=GUARD + timedelta(minutes=30),
            origin=HOME,
            destination="Nashville Office",
            baseline_seconds=1800,
            anchor=GUARD + timedelta(minutes=30),
        ),
        DesiredBlock(
            identity="pre-2",
            kind="meeting_return",
            summary="Drive: Late office",
            start=READY + timedelta(minutes=15),
            end=READY + timedelta(minutes=45),
            origin="Nashville Office",
            destination=HOME,
            baseline_seconds=1800,
            anchor=READY + timedelta(minutes=15),
        ),
    ]
    result = _plan(extra_desired=extra, postarrival_meetings=[])
    created = _created(result)
    assert not any(b.identity == "pre-2" for b in created)
    assert all(h.identity != "pre-2" for h in result.held)


# --- scan → meeting_source → composer → reconcile regressions ---------------


def _gcal_event(event_id, start, end, location, **extra):
    event = {
        "id": event_id,
        "summary": event_id,
        "location": location,
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Denver"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Denver"},
    }
    event.update(extra)
    return event


def _covering_unaddressed_schedule():
    records = _schedule(lodging=False)
    records[0]["end"] = "2020-07-16"
    records.extend(
        [
            {
                "type": "Lodging",
                "summary": "Check-in: Nameless Inn",
                "start": "2020-07-12T15:00:00Z",
            },
            {
                "type": "Lodging",
                "summary": "Check-out: Nameless Inn",
                "start": "2020-07-14T15:00:00Z",
            },
            {
                "type": "Lodging",
                "summary": "Check-in: Boulder Lodge",
                "start": "2020-07-14T18:00:00Z",
                "location": "Boulder Lodge Address",
            },
            {
                "type": "Lodging",
                "summary": "Check-out: Boulder Lodge",
                "start": "2020-07-15T15:00:00Z",
                "location": "Boulder Lodge Address",
            },
        ]
    )
    return records


def _scan_plan(
    events,
    *,
    now=NOW,
    skip_state=None,
    current_blocks=(),
    schedule=None,
    route=None,
):
    """Real scan → meeting_source → composer → reconcile (no injected meetings)."""
    schedule = list(schedule or _schedule())
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=frozenset({"BNA"}))

    def anchor_for(at):
        return meeting_anchor(itinerary, at)

    skips = skip_state or {}
    meetings = scan(
        events,
        now=now,
        home_address=HOME,
        skip_state=skips,
        anchor_for=anchor_for,
        flight_windows=[],
        flight_summaries=[],
    )
    blocks, _skipped, held = meeting_desired_blocks(meetings, route=route or ISSUE_ROUTE)
    return _plan(
        extra_desired=blocks,
        extra_held=held,
        now=now,
        schedule=schedule,
        current_blocks=list(current_blocks),
        route=route or ISSUE_ROUTE,
        postarrival_meetings=postarrival_meetings_from(meetings, skip_state=skips, now=now),
        itinerary=itinerary,
    )


def _created_kinds(result, identity):
    return {b.kind for b in _created(result) if b.identity == identity}


def test_scan_declined_meeting_keeps_airport_to_lodging():
    result = _scan_plan(
        [
            _gcal_event(
                "declined",
                MEETING_START,
                MEETING_END,
                VENUE,
                attendees=[{"self": True, "responseStatus": "declined"}],
            )
        ]
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not _created_kinds(result, "declined")


def test_scan_cancelled_meeting_keeps_airport_to_lodging():
    result = _scan_plan(
        [_gcal_event("cancelled", MEETING_START, MEETING_END, VENUE, status="cancelled")]
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not _created_kinds(result, "cancelled")


def test_scan_skipped_meeting_keeps_airport_to_lodging():
    result = _scan_plan(
        [_gcal_event("skipme", MEETING_START, MEETING_END, VENUE)],
        skip_state={"skipme": "2020-08-01T00:00:00+00:00"},
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not _created_kinds(result, "skipme")


def test_scan_virtual_meeting_does_not_block_real_handoff():
    result = _scan_plan(
        [
            _gcal_event("real", MEETING_START, MEETING_END, VENUE),
            _gcal_event("zoom", MEETING_START, MEETING_END, "https://zoom.us/j/1"),
        ]
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == VENUE
    assert not _created_kinds(result, "zoom")


def test_scan_anchor_meeting_keeps_airport_to_lodging():
    result = _scan_plan([_gcal_event("hoteldinner", MEETING_START, MEETING_END, HOTEL)])
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not _created_kinds(result, "hoteldinner")


def test_scan_planner_block_does_not_become_a_handoff():
    result = _scan_plan(
        [
            _gcal_event(
                "planner",
                MEETING_START,
                MEETING_END,
                VENUE,
                description="[drive-planner:meeting=mtg-1:dir=outbound]",
            )
        ]
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not _created_kinds(result, "planner")


def test_scan_inflight_past_inperson_meeting_still_hands_off():
    inflight = datetime(2020, 7, 12, 21, 0, tzinfo=UTC)
    result = _scan_plan(
        [_gcal_event("mtg-1", MEETING_START, MEETING_END, VENUE)],
        now=inflight,
    )
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == VENUE
    ret = next(b for b in created if b.kind == "meeting_return")
    assert ret.identity == "mtg-1"
    assert ret.destination == HOTEL
    assert not any(b.kind == "meeting_outbound" for b in created)


def test_scan_active_skip_on_past_bucket_keeps_airport_to_lodging():
    inflight = datetime(2020, 7, 12, 21, 0, tzinfo=UTC)
    result = _scan_plan(
        [_gcal_event("skippast", MEETING_START, MEETING_END, VENUE)],
        now=inflight,
        skip_state={"skippast": "2020-08-01T00:00:00+00:00"},
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not _created_kinds(result, "skippast")


def test_scan_bridge_next_meeting_suppresses_hotel_return():
    next_start = MEETING_END + timedelta(minutes=10)
    result = _scan_plan(
        [
            _gcal_event("A", MEETING_START, MEETING_END, VENUE),
            _gcal_event("B", next_start, next_start + timedelta(hours=1), VENUE_B),
        ]
    )
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == VENUE
    assert not any(b.identity == "A" and b.kind == "meeting_return" for b in created)
    assert not any(b.origin == VENUE and b.destination == HOTEL for b in created)


def test_scan_same_venue_next_meeting_suppresses_hotel_return():
    next_start = MEETING_END + timedelta(minutes=10)
    result = _scan_plan(
        [
            _gcal_event("A", MEETING_START, MEETING_END, VENUE),
            _gcal_event("B", next_start, next_start + timedelta(hours=1), VENUE),
        ]
    )
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == VENUE
    assert not any(b.identity == "A" and b.kind == "meeting_return" for b in created)


def test_scan_predeparture_protected_return_keeps_outbound():
    start = datetime(2020, 7, 12, 14, 0, tzinfo=UTC)
    end = datetime(2020, 7, 12, 16, 30, tzinfo=UTC)
    result = _scan_plan([_gcal_event("pre", start, end, "Nashville Office")])
    created = _created(result)
    assert any(b.identity == "pre" and b.kind == "meeting_outbound" for b in created)
    assert not any(b.identity == "pre" and b.kind == "meeting_return" for b in created)


def test_scan_held_protected_return_is_deleted_past_outbound_is_history():
    inflight = datetime(2020, 7, 12, 21, 0, tzinfo=UTC)
    end = datetime(2020, 7, 12, 22, 0, tzinfo=UTC)
    current = [
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="out-stale",
            identity="late",
            kind="meeting_outbound",
            origin=CITY,
            destination=VENUE,
            baseline_seconds=1140,
            anchor=MEETING_START,
        ),
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="ret-future",
            identity="late",
            kind="meeting_return",
            origin=VENUE,
            destination=HOTEL,
            baseline_seconds=1440,
            anchor=end,
        ),
    ]
    result = _scan_plan(
        [_gcal_event("late", MEETING_START, end, VENUE)],
        now=inflight,
        current_blocks=current,
    )
    deleted = {d.event_id for d in result.plan.deletes}
    assert "ret-future" in deleted
    assert "out-stale" not in deleted
    assert all(h.identity != "late" for h in result.held)
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL


def test_scan_all_route_failure_does_not_retain_airborne_meeting_block():
    current = [
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="out-stale",
            identity="mtg-1",
            kind="meeting_outbound",
            origin=CITY,
            destination=VENUE,
            baseline_seconds=1140,
            anchor=MEETING_START,
        )
    ]
    result = _scan_plan(
        [_gcal_event("mtg-1", MEETING_START, MEETING_END, VENUE)],
        current_blocks=current,
        route=_route({}, default_minutes=None),
    )
    assert all(h.identity != "mtg-1" for h in result.held)
    assert any(d.event_id == "out-stale" for d in result.plan.deletes)
    assert not any(b.kind == "airport_arrival" for b in _created(result))


def test_scan_return_route_failure_keeps_airport_to_lodging():
    result = _scan_plan(
        [_gcal_event("mtg-1", MEETING_START, MEETING_END, VENUE)],
        route=_route(
            {("DEN airport", VENUE): 37, ("DEN airport", HOTEL): 37, (VENUE, HOTEL): None}
        ),
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not _created_kinds(result, "mtg-1")
    assert any("venue-to-lodging route failed" in s for s in result.skipped)


def test_scan_covering_unaddressed_stay_does_not_return_to_later_hotel():
    result = _scan_plan(
        [_gcal_event("mtg-1", MEETING_START, MEETING_END, VENUE)],
        schedule=_covering_unaddressed_schedule(),
        route=_route(
            {
                ("DEN airport", VENUE): 37,
                ("DEN airport", CITY): 37,
                (VENUE, CITY): 24,
                (HOME, "BNA airport"): 30,
            }
        ),
    )
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == VENUE
    ret = next(b for b in created if b.kind == "meeting_return")
    assert ret.destination != "Boulder Lodge Address"
    assert ret.destination == CITY


# --- N1: reachability is judged per candidate before ambiguity ----------------

NASHVILLE = "Nashville Office"
INFLIGHT = datetime(2020, 7, 12, 21, 0, tzinfo=UTC)


def _far_route():
    return _route(
        {
            ("DEN airport", VENUE): 37,
            ("DEN airport", HOTEL): 37,
            (VENUE, HOTEL): 24,
            (CITY, VENUE): 19,
            (HOTEL, VENUE): 19,
            ("DEN airport", NASHVILLE): 17 * 60,
            (CITY, NASHVILLE): 17 * 60,
            (HOTEL, NASHVILLE): 17 * 60,
            (NASHVILLE, CITY): 17 * 60,
            (NASHVILLE, HOTEL): 17 * 60,
            (HOME, "BNA airport"): 30,
        }
    )


def _assert_reachable_meeting_replaces_arrival(result):
    created = _created(result)
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == VENUE
    assert arrival.origin == "DEN airport"
    ret = next(b for b in created if b.identity == "real" and b.kind == "meeting_return")
    assert ret.destination == HOTEL
    assert not any(b.identity == "real" and b.kind == "meeting_outbound" for b in created)
    assert not _created_kinds(result, "home-weekly")
    assert all(not (GUARD <= b.start < READY) for b in created)


def test_scan_unreachable_far_meeting_does_not_make_reachable_one_ambiguous():
    for now in (NOW, INFLIGHT):
        result = _scan_plan(
            [
                _gcal_event("real", MEETING_START, MEETING_END, VENUE),
                _gcal_event("home-weekly", MEETING_START, MEETING_END, NASHVILLE),
            ],
            now=now,
            route=_far_route(),
        )
        _assert_reachable_meeting_replaces_arrival(result)
        assert any("exceeds cap" in s for s in result.skipped)
        assert not any("no automatic choice" in s for s in result.skipped)


def test_scan_unreachable_short_meeting_does_not_make_reachable_one_ambiguous():
    short_end = datetime(2020, 7, 12, 22, 45, tzinfo=UTC)
    result = _scan_plan(
        [
            _gcal_event("real", MEETING_START, MEETING_END, VENUE),
            _gcal_event("home-weekly", MEETING_START, short_end, VENUE_B),
        ]
    )
    _assert_reachable_meeting_replaces_arrival(result)
    assert any("not reachable" in s for s in result.skipped)


def test_scan_unreachable_candidate_blocks_are_deleted_not_held():
    stale = ParsedBlock(
        generation=GEN_UNIFIED,
        event_id="ret-weekly",
        identity="home-weekly",
        kind="meeting_return",
        origin=NASHVILLE,
        destination=HOME,
        baseline_seconds=600,
        anchor=MEETING_END,
    )
    result = _scan_plan(
        [
            _gcal_event("real", MEETING_START, MEETING_END, VENUE),
            _gcal_event("home-weekly", MEETING_START, MEETING_END, NASHVILLE),
        ],
        current_blocks=[stale],
        route=_far_route(),
    )
    assert any(d.event_id == "ret-weekly" for d in result.plan.deletes)
    assert all(h.identity != "home-weekly" for h in result.held)


def test_scan_route_failed_candidate_stays_in_play_and_keeps_failsafe():
    result = _scan_plan(
        [
            _gcal_event("real", MEETING_START, MEETING_END, VENUE),
            _gcal_event("unknown", MEETING_START, MEETING_END, VENUE_B),
        ],
        route=_route(
            {
                ("DEN airport", VENUE): 37,
                ("DEN airport", VENUE_B): None,
                ("DEN airport", HOTEL): 37,
                (HOTEL, VENUE_B): None,
                (VENUE, HOTEL): 24,
                (HOME, "BNA airport"): 30,
            },
            default_minutes=None,
        ),
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert not _created_kinds(result, "real")
    assert any("2 overlapping meetings; no automatic choice" in s for s in result.skipped)


def test_scan_two_reachable_meetings_still_make_no_automatic_choice():
    result = _scan_plan(
        [
            _gcal_event("real", MEETING_START, MEETING_END, VENUE),
            _gcal_event("also", MEETING_START, MEETING_END, VENUE_B),
        ]
    )
    arrival = next(b for b in _created(result) if b.kind == "airport_arrival")
    assert arrival.destination == HOTEL
    assert any("2 overlapping meetings; no automatic choice" in s for s in result.skipped)


# --- N2: a held leg is judged by the drive start of the block it keeps --------


def _outbound_current(event_id, identity, *, anchor, minutes, origin, destination):
    return ParsedBlock(
        generation=GEN_UNIFIED,
        event_id=event_id,
        identity=identity,
        kind="meeting_outbound",
        origin=origin,
        destination=destination,
        baseline_seconds=minutes * 60,
        anchor=anchor,
    )


def test_scan_held_outbound_starting_in_window_is_deleted_when_it_arrives_after_ready():
    soon_start = _dt(22, 45)
    current = [
        _outbound_current(
            "out-protected",
            "soon",
            anchor=soon_start,
            minutes=30,
            origin=HOTEL,
            destination=VENUE_B,
        )
    ]
    result = _scan_plan(
        [_gcal_event("soon", soon_start, _dt(23, 45), VENUE_B)],
        current_blocks=current,
        route=_route(
            {("DEN airport", HOTEL): 37, (HOTEL, VENUE_B): None, (HOME, "BNA airport"): 30},
            default_minutes=None,
        ),
    )
    assert any(d.event_id == "out-protected" for d in result.plan.deletes)
    assert all(h.identity != "soon" for h in result.held)
    assert not _created_kinds(result, "soon")


def test_scan_held_outbound_leaving_before_guard_is_retained_on_route_failure():
    start, end = _dt(16, 30), _dt(16, 50)
    ok_route = _route(
        {
            (HOME, "Airport Office"): 20,
            ("Airport Office", HOME): 20,
            (HOME, "BNA airport"): 30,
            ("BNA airport", HOME): 30,
            ("DEN airport", HOTEL): 37,
        },
        default_minutes=None,
    )
    routed = _scan_plan([_gcal_event("clr", start, end, "Airport Office")], route=ok_route)
    outbound = [b for b in _created(routed) if b.identity == "clr" and b.kind == "meeting_outbound"]
    assert len(outbound) == 1 and outbound[0].start < GUARD <= start
    current = [
        _outbound_current(
            "clr-out",
            "clr",
            anchor=outbound[0].anchor,
            minutes=int(outbound[0].baseline_seconds // 60),
            origin=outbound[0].origin,
            destination=outbound[0].destination,
        )
    ]
    failed = _scan_plan(
        [_gcal_event("clr", start, end, "Airport Office")],
        current_blocks=current,
        route=_route(
            {(HOME, "BNA airport"): 30, ("BNA airport", HOME): 30, ("DEN airport", HOTEL): 37},
            default_minutes=None,
        ),
    )
    assert not any(d.event_id == "clr-out" for d in failed.plan.deletes)
    assert not any(c.desired.identity == "clr" for c in failed.plan.creates)
    assert any(h.key == ("clr", "meeting_outbound") for h in failed.held)


def test_scan_held_return_starting_in_window_is_deleted_even_without_a_block():
    inflight_end = _dt(22, 0)
    result = _scan_plan(
        [_gcal_event("late", MEETING_START, inflight_end, VENUE)],
        now=INFLIGHT,
    )
    assert all(h.key != ("late", "meeting_return") for h in result.held)
    assert any("late: drive would start inside a flight interval" in s for s in result.skipped)


def test_scan_held_outbound_history_stays_distinct_from_future_return():
    current = [
        _outbound_current(
            "out-history",
            "late",
            anchor=MEETING_START,
            minutes=19,
            origin=CITY,
            destination=VENUE,
        ),
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="ret-future",
            identity="late",
            kind="meeting_return",
            origin=VENUE,
            destination=HOTEL,
            baseline_seconds=1440,
            anchor=_dt(22, 0),
        ),
    ]
    result = _scan_plan(
        [_gcal_event("late", MEETING_START, _dt(22, 0), VENUE)],
        now=INFLIGHT,
        current_blocks=current,
    )
    deleted = {d.event_id for d in result.plan.deletes}
    assert "ret-future" in deleted
    assert "out-history" not in deleted


# --- NEW-2: ordinary arrival-day meeting before check-in never uses the centroid


LATE_HOTEL = "Late Hotel Address"


def _late_checkin_schedule():
    records = _schedule(lodging=False)
    records.extend(
        [
            {
                "type": "Lodging",
                "summary": "Check-in: Late Hotel",
                "start": "2020-07-13T02:00:00Z",
                "location": LATE_HOTEL,
            },
            {
                "type": "Lodging",
                "summary": "Check-out: Late Hotel",
                "start": "2020-07-14T15:00:00Z",
                "location": LATE_HOTEL,
            },
        ]
    )
    return records


def _late_checkin_route():
    return _route(
        {
            ("DEN airport", CITY): 37,
            ("DEN airport", LATE_HOTEL): 37,
            (CITY, VENUE): 19,
            (VENUE, CITY): 24,
            (LATE_HOTEL, VENUE): 19,
            (VENUE, LATE_HOTEL): 24,
            (HOME, "BNA airport"): 30,
        },
        default_minutes=None,
    )


def test_scan_arrival_day_meeting_before_checkin_uses_addressed_lodging_not_centroid():
    start = _dt(23, 0)
    for now in (NOW, INFLIGHT):
        result = _scan_plan(
            [_gcal_event("pc", start, start + timedelta(hours=1), VENUE)],
            schedule=_late_checkin_schedule(),
            route=_late_checkin_route(),
            now=now,
        )
        legs = [(b.kind, b.origin, b.destination) for b in _created(result) if b.identity == "pc"]
        assert ("meeting_outbound", LATE_HOTEL, VENUE) in legs
        assert ("meeting_return", VENUE, LATE_HOTEL) in legs
        assert all(CITY not in (origin, dest) for _, origin, dest in legs)


def test_meeting_anchor_keeps_covering_stay_and_trip_location_without_lodging():
    covering = Itinerary(_schedule(), home_address=HOME, home_airports=frozenset({"BNA"}))
    assert meeting_anchor(covering, _dt(23, 0))[0] == HOTEL
    nameless = Itinerary(
        _covering_unaddressed_schedule(), home_address=HOME, home_airports=frozenset({"BNA"})
    )
    assert meeting_anchor(nameless, _dt(23, 0))[0] == CITY
    no_lodging = Itinerary(
        _schedule(lodging=False), home_address=HOME, home_airports=frozenset({"BNA"})
    )
    assert meeting_anchor(no_lodging, _dt(23, 0))[0] == CITY


def test_meeting_anchor_does_not_pick_a_later_hotel_after_an_unaddressed_stay():
    schedule = _schedule(lodging=False)
    schedule[0]["end"] = "2020-07-16"
    schedule.extend(
        [
            {
                "type": "Lodging",
                "summary": "Check-in: First Inn",
                "start": "2020-07-12T15:00:00Z",
            },
            {
                "type": "Lodging",
                "summary": "Check-out: First Inn",
                "start": "2020-07-13T15:00:00Z",
            },
            {
                "type": "Lodging",
                "summary": "Check-in: Second Inn",
                "start": "2020-07-15T15:00:00Z",
                "location": "Second Inn Address",
            },
            {
                "type": "Lodging",
                "summary": "Check-out: Second Inn",
                "start": "2020-07-16T15:00:00Z",
                "location": "Second Inn Address",
            },
        ]
    )
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=frozenset({"BNA"}))
    assert meeting_anchor(itinerary, _dt(12, 0, day=14))[0] == CITY
