"""Trains get real station legs in the same calendar reconcile as meetings."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for bundle in ("travel-core", "flight-assist", "drive-engine"):
    sys.path.insert(0, str(ROOT / "skills" / bundle))

from block_codec import parse_block  # noqa: E402
from calendar_apply import build_create_args  # noqa: E402
from engine import build_reconcile_plan  # noqa: E402
from itinerary import Itinerary  # noqa: E402
from meeting_source import meeting_desired_blocks  # noqa: E402
from postarrival import PostarrivalMeeting, postarrival_meetings_from  # noqa: E402
from rail_source import station_desired_blocks  # noqa: E402
from reconcile import DesiredBlock  # noqa: E402
from reconcile_sweep import meeting_anchor  # noqa: E402
from scan import scan  # noqa: E402

NOW = datetime(2020, 8, 7, 12, tzinfo=UTC)
DEP = NOW + timedelta(days=1, hours=2)
ARR = DEP + timedelta(hours=4)


def schedule():
    return [
        {
            "type": "Trip",
            "summary": "Rail visit",
            "start": DEP.date().isoformat(),
            "end": (DEP + timedelta(days=2)).date().isoformat(),
            "location": "Destination city",
        },
        {
            "type": "Rail",
            "uid": "item-rail@tripit.com",
            "summary": "Train A to B",
            "start": DEP.isoformat(),
            "end": ARR.isoformat(),
            "location": "Origin Station",
            "description": "[Rail] A to B\\nDepart Origin Station\\nArrive Destination Station\\n ",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hotel",
            "start": (ARR + timedelta(hours=1)).isoformat(),
            "location": "Hotel address",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Hotel",
            "start": (ARR + timedelta(days=1)).isoformat(),
            "location": "Hotel address",
        },
    ]


def route(origin, destination):
    return {
        ("Home", "Origin Station"): timedelta(minutes=30),
        ("Destination Station", "Hotel address"): timedelta(minutes=20),
        ("Destination Station", "Venue"): timedelta(minutes=15),
        ("Hotel address", "Venue"): timedelta(minutes=20),
        ("Venue", "Hotel address"): timedelta(minutes=20),
    }.get((origin, destination))


def test_station_blocks_use_real_addresses_and_reconcile_without_duplicates():
    rows = schedule()
    result = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=[],
        route=route,
        schedule=rows,
        home_address="Home",
        now=NOW,
    )
    blocks = [create.desired for create in result.plan.creates]
    assert [(b.kind, b.origin, b.destination, b.start, b.end) for b in blocks] == [
        (
            "station_departure",
            "Home",
            "Origin Station",
            DEP - timedelta(hours=1),
            DEP - timedelta(minutes=30),
        ),
        (
            "station_arrival",
            "Destination Station",
            "Hotel address",
            ARR,
            ARR + timedelta(minutes=20),
        ),
    ]
    current = []
    for i, block in enumerate(blocks):
        parsed = parse_block(build_create_args(block, calendar_id="primary") | {"id": f"s{i}"})
        assert parsed is not None
        current.append(parsed)
    again = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=current,
        route=route,
        schedule=rows,
        home_address="Home",
        now=NOW,
    )
    assert again.plan.is_noop
    without_booking = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=current,
        route=route,
        schedule=[],
        home_address="Home",
        now=NOW,
    )
    assert {d.event_id for d in without_booking.plan.deletes} == {"s0", "s1"}


@pytest.mark.parametrize("different_station", [False, True])
def test_connecting_trains_have_no_phantom_home_or_hotel_drive(different_station):
    rows = schedule()
    middle = DEP + timedelta(hours=2)
    connection = "Connection East" if different_station else "Connection Station"
    onward = rows[1] | {
        "uid": "item-onward@tripit.com",
        "summary": "Train B to C",
        "start": (middle + timedelta(hours=1)).isoformat(),
        "description": f"Depart {connection}\\nArrive Destination Station",
    }
    rows[1] = rows[1] | {
        "end": middle.isoformat(),
        "description": "Depart Origin Station\\nArrive Connection Station",
    }
    rows.append(onward)

    def connecting_route(a, b):
        if a == "Connection Station" and b in {"Connection East", "Venue"}:
            return timedelta(minutes=15)
        return route(a, b)

    unsafe = DesiredBlock(
        identity="unsafe-meeting",
        kind="meeting_outbound",
        summary="Drive: unsafe meeting",
        start=DEP + timedelta(minutes=15),
        end=DEP + timedelta(minutes=30),
        origin="Home",
        destination="Venue",
        baseline_seconds=900,
        anchor=DEP + timedelta(minutes=30),
    )
    result = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=[],
        schedule=rows,
        home_address="Home",
        route=connecting_route,
        now=NOW,
        extra_desired=[unsafe],
        postarrival_meetings=[
            PostarrivalMeeting(
                "connection-meeting",
                "Meeting during connection",
                "Venue",
                middle - timedelta(minutes=10),
                middle + timedelta(minutes=45),
            )
        ],
    )
    blocks = [create.desired for create in result.plan.creates]
    assert result.held == ()
    assert all(b.identity != "unsafe-meeting" for b in blocks)
    endpoints = [(b.origin, b.destination) for b in blocks]
    expected = [("Home", "Origin Station")]
    if different_station:
        expected.append(("Connection Station", "Connection East"))
        transfer = next(b for b in blocks if b.destination == "Connection East")
        assert transfer.start == middle
        assert transfer.end == middle + timedelta(minutes=15)
    expected.append(("Destination Station", "Hotel address"))
    assert endpoints == expected


def test_grounded_lodging_between_trains_keeps_its_real_drives():
    rows = schedule()
    rows.append(
        rows[1]
        | {
            "uid": "item-onward@tripit.com",
            "summary": "Train B to C",
            "start": (ARR + timedelta(hours=26)).isoformat(),
            "end": (ARR + timedelta(hours=28)).isoformat(),
            "description": "Depart Destination Station\\nArrive Third Station",
        }
    )

    def hotel_route(a, b):
        return (
            timedelta(minutes=20)
            if (a, b) == ("Hotel address", "Destination Station")
            else route(a, b)
        )

    blocks, _, _, _ = station_desired_blocks(
        rows,
        itinerary=Itinerary(rows, home_address="Home"),
        route=hotel_route,
        now=NOW,
        window=timedelta(days=14),
    )
    endpoints = [(b.origin, b.destination) for b in blocks]
    assert ("Destination Station", "Hotel address") in endpoints
    assert ("Hotel address", "Destination Station") in endpoints


def test_train_arrival_reuses_the_meeting_handoff():
    rows = schedule()
    itinerary = Itinerary(rows, home_address="Home")
    event = {
        "id": "meeting",
        "summary": "Meeting",
        "location": "Venue",
        "start": {"dateTime": (ARR - timedelta(minutes=10)).isoformat()},
        "end": {"dateTime": (ARR + timedelta(hours=1)).isoformat()},
    }
    meetings = scan(
        [event],
        now=NOW,
        home_address="Home",
        skip_state={},
        anchor_for=lambda at: meeting_anchor(itinerary, at),
    )
    local, _, held = meeting_desired_blocks(meetings, route=route)
    result = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=[],
        route=route,
        schedule=rows,
        itinerary=itinerary,
        now=NOW,
        extra_desired=local,
        extra_held=held,
        postarrival_meetings=postarrival_meetings_from(meetings, now=NOW),
    )
    blocks = [c.desired for c in result.plan.creates]
    arrival = next(b for b in blocks if b.kind == "station_arrival")
    assert (arrival.origin, arrival.destination, arrival.start, arrival.end) == (
        "Destination Station",
        "Venue",
        ARR,
        ARR + timedelta(minutes=15),
    )
    assert not any(b.kind == "meeting_outbound" for b in blocks)
    back = next(b for b in blocks if b.kind == "meeting_return")
    assert (back.origin, back.destination) == ("Venue", "Hotel address")


def test_missing_arrival_address_is_held_without_inventing_an_endpoint():
    rows = schedule()
    rows[1]["description"] = "Depart Origin Station"
    blocks, held, _, _ = station_desired_blocks(
        rows,
        itinerary=Itinerary(rows, home_address="Home"),
        route=route,
        now=NOW,
        window=timedelta(days=14),
        display_tz="America/Chicago",
    )
    assert [b.kind for b in blocks] == ["station_departure"]
    assert blocks[0].timezone == "America/Chicago"
    assert blocks[0].zone_from_operator is True
    assert [h.kind for h in held] == ["station_arrival"]


def test_unreadable_train_times_hold_its_blocks_and_skip_with_a_diagnostic():
    rows = schedule()
    good = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=[],
        route=route,
        schedule=rows,
        home_address="Home",
        now=NOW,
    )
    current = []
    for i, create in enumerate(good.plan.creates):
        block = parse_block(
            build_create_args(create.desired, calendar_id="primary") | {"id": f"old{i}"}
        )
        assert block is not None
        current.append(block)
    rows[1]["end"] = "unreadable"
    result = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=current,
        route=route,
        schedule=rows,
        home_address="Home",
        now=NOW,
    )
    assert result.plan.is_noop
    assert {leg.kind for leg in result.held} == {"station_departure", "station_arrival"}
    assert any("refresh travel-schedule" in line for line in result.skipped)
