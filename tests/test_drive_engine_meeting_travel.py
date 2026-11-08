"""Long-drive bookings use actual arrival places, never any flight in the window."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "travel-core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "drive-engine"))

from itinerary import CANCELLED, Itinerary, Transport  # noqa: E402
from meeting_travel import booked_transport_to, maintain_meeting_routes  # noqa: E402

AT = datetime(2020, 7, 13, 16, tzinfo=UTC)


def flight(origin="BNA", destination="ATL", hours_before=5):
    return {
        "type": "Flight",
        "summary": f"DL 123 {origin} to {destination}",
        "start": (AT - timedelta(hours=hours_before)).isoformat(),
        "end": (AT - timedelta(hours=hours_before - 1)).isoformat(),
    }


def check(schedule, pairs):
    return booked_transport_to(
        destination="Pickup",
        at=AT,
        until=AT + timedelta(hours=1),
        schedule=schedule,
        itinerary=Itinerary(schedule, home_address=None),
        route=lambda a, b: pairs.get((a, b)),
    )


def test_flight_to_the_meeting_area_is_booked_transport():
    assert check([flight()], {("ATL airport", "Pickup"): timedelta(minutes=12)})


def test_a_flight_elsewhere_does_not_cancel_the_drive():
    assert not check(
        [flight(destination="LAX")],
        {
            ("LAX airport", "Pickup"): timedelta(hours=32),
        },
    )


def test_an_unavailable_arrival_route_is_unknown_rather_than_no_booking():
    assert check([flight()], {}) is None


def test_return_booking_wins_the_earlier_flight_away():
    assert not check(
        [flight(hours_before=20), flight("ATL", "BNA", hours_before=5)],
        {
            ("ATL airport", "Pickup"): timedelta(minutes=12),
            ("BNA airport", "Pickup"): timedelta(hours=4),
        },
    )


def test_a_booking_after_the_meeting_does_not_cancel_the_drive():
    assert not check([flight(hours_before=-2)], {})


def test_an_old_flight_does_not_cancel_the_drive():
    assert not check([flight(hours_before=80)], {})


@pytest.mark.parametrize("arrival_minutes", [61, 55, 48])
def test_transport_that_cannot_reach_the_meeting_before_it_ends_is_not_coverage(arrival_minutes):
    row = flight()
    row["end"] = (AT + timedelta(minutes=arrival_minutes)).isoformat()
    # The meeting ends at AT + 1h; the local connection takes twelve minutes.
    assert not check([row], {("ATL airport", "Pickup"): timedelta(minutes=12)})


def test_rail_uses_the_arrival_station_instead_of_location():
    # Observed TripIt shape: LOCATION is the departure station.
    rail = {
        "type": "Rail",
        "summary": "State Railway - City A to City B",
        "start": (AT - timedelta(hours=15)).isoformat(),
        "end": (AT - timedelta(hours=4)).isoformat(),
        "location": "Origin Station, Region A",
        "description": "[Rail] City A to City B\\nDepart Origin Station"
        "\\n5:23 AM +07\\nArrive Destination Station\\, Region B\\n ",
    }
    assert check([rail], {("Destination Station, Region B", "Pickup"): timedelta(minutes=8)})


def test_untimed_transport_is_not_a_booked_journey():
    assert not check([{"type": "Flight", "start": "2020-07-13", "end": "2020-07-13"}], {})


def test_assumption_reply_booking_and_existing_notice_work_together(check_travel_bookings):
    from types import SimpleNamespace

    from answer_drive_or_fly import record_answer
    from block_codec import parse_block
    from calendar_apply import apply_plan, build_create_args
    from drive_decision import load_verdicts, meeting_key
    from meeting_source import meeting_desired_blocks
    from reconcile import plan_reconcile
    from reconcile_sweep import build_sweep_payload
    from scan import scan

    now = AT - timedelta(days=1)
    event = {
        "id": "pickup",
        "summary": "Asher pickup",
        "location": "Pickup",
        "start": {"dateTime": AT.isoformat()},
        "end": {"dateTime": (AT + timedelta(hours=1)).isoformat()},
    }
    meetings = scan([event], now=now, home_address="Home", skip_state={})

    def route(a, b):
        return timedelta(minutes=12 if a == "ATL airport" else 227)

    blocks, _, held = meeting_desired_blocks(meetings, route=route)
    assert len(blocks) == 2 and not held
    maintain_meeting_routes(
        meetings,
        route=route,
        booked=set(),
        driving_to={},
        verdicts={},
        no_routes=set(),
        now=now,
    )
    existing = []
    for i, block in enumerate(blocks):
        raw = build_create_args(block, calendar_id="primary") | {"id": f"old{i}"}
        parsed = parse_block(raw)
        assert parsed is not None
        existing.append(parsed)
    applied = apply_plan(
        plan_reconcile(blocks, [], now=now),
        calendar=SimpleNamespace(create_event=lambda _: {"id": "created"}),
        now=now,
        creation_clock=lambda: now,
    )
    message = build_sweep_payload(applied, [])["data"]["message"]
    assert "Assumed driving" in message and "reply 'fly Asher pickup'" in message
    result = record_answer(
        {"trip": "Asher pickup", "answer": "fly"},
        schedule=[],
        events=[event],
        now=now,
    )
    assert result["recorded"] is True
    booking_check, _, _ = check_travel_bookings
    assert (
        booking_check.load_meeting_transport_gaps(
            now=now, events=[event], schedule=[], route=route
        )[0][0]["trip"]
        == "Asher pickup"
    )
    # The later booking wins even after a subsequent explicit drive answer.
    record_answer({"trip": "Asher pickup", "answer": "drive"}, schedule=[], events=[event], now=now)
    booked = booked_transport_to(
        destination="Pickup",
        at=AT,
        until=AT + timedelta(hours=1),
        schedule=[flight()],
        itinerary=Itinerary([flight()], home_address="Home"),
        route=route,
    )
    assert booked
    after_booking, _, held = meeting_desired_blocks(
        meetings,
        route=route,
        booked_transport=lambda _: booked,
    )
    assert after_booking == held == []
    plan = plan_reconcile(after_booking, existing, now=now)
    assert {d.event_id for d in plan.deletes} == {"old0", "old1"}
    maintain_meeting_routes(
        meetings,
        route=route,
        booked={"pickup"},
        driving_to={},
        verdicts=load_verdicts(now),
        no_routes=set(),
        now=now,
    )
    assert meeting_key("pickup") not in load_verdicts(now)
    assert booking_check.load_meeting_transport_gaps(now=now) == ([], None)


def test_cancelled_preferred_flight_does_not_return_from_the_stale_tripit_copy():
    row = flight()
    itinerary = Itinerary(
        [row],
        home_address="Home",
        transports=[
            Transport(
                departs=datetime.fromisoformat(row["start"]),
                arrives=datetime.fromisoformat(row["end"]),
                origin="BNA",
                destination="ATL",
                status=CANCELLED,
            )
        ],
    )
    assert (
        booked_transport_to(
            destination="Pickup",
            at=AT,
            until=AT + timedelta(hours=1),
            schedule=[row],
            itinerary=itinerary,
            route=lambda a, b: timedelta(minutes=12),
        )
        is False
    )


@pytest.mark.parametrize(
    "durations,no_routes,expects_gap",
    [
        ((timedelta(hours=9), timedelta(hours=9)), set(), True),
        ((None, None), {("Home", "Venue"), ("Venue", "Home")}, True),
        ((None, None), set(), False),
        ((timedelta(hours=9), None), set(), True),
        ((None, timedelta(hours=9)), set(), True),
    ],
)
def test_long_or_unroutable_meeting_goes_to_booking_check_but_an_outage_does_not(
    check_travel_bookings,
    durations,
    no_routes,
    expects_gap,
):
    from drive_decision import clear_meeting_route
    from scan import scan

    now = AT - timedelta(days=1)
    event = {
        "id": "distant",
        "summary": "Distant meeting",
        "location": "Venue",
        "start": {"dateTime": AT.isoformat()},
        "end": {"dateTime": (AT + timedelta(hours=1)).isoformat()},
    }
    meetings = scan([event], now=now, home_address="Home", skip_state={})
    check_bookings, _, _ = check_travel_bookings
    clear_meeting_route("distant")
    maintain_meeting_routes(
        meetings,
        route=lambda a, b: durations[0] if a == "Home" else durations[1],
        booked=set(),
        driving_to={},
        verdicts={},
        no_routes=no_routes,
        now=now,
    )
    gaps, error = check_bookings.load_meeting_transport_gaps(
        now=now, events=[event], schedule=[], route=lambda a, b: None
    )
    assert error is None
    assert bool(gaps) is expects_gap
    if gaps:
        assert gaps[0]["trip"] == "Distant meeting"


def test_one_known_four_hour_route_still_records_context_for_the_advertised_reply(
    monkeypatch, tmp_path
):
    from answer_drive_or_fly import record_answer
    from drive_decision import load_verdicts, meeting_key
    from scan import scan

    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    now = AT - timedelta(days=1)
    event = {
        "id": "asymmetric",
        "summary": "Distant meeting",
        "location": "Venue",
        "start": {"dateTime": AT.isoformat()},
        "end": {"dateTime": (AT + timedelta(hours=1)).isoformat()},
    }
    meetings = scan([event], now=now, home_address="Home", skip_state={})
    maintain_meeting_routes(
        meetings,
        route=lambda a, b: timedelta(hours=4) if a == "Home" else None,
        booked=set(),
        driving_to={},
        verdicts={},
        no_routes=set(),
        now=now,
    )
    context = load_verdicts(now)[meeting_key("asymmetric")]
    assert context.drive_seconds == 14400
    assert context.meeting is not None and context.meeting["gap_reason"] is None
    answer = record_answer(
        {"trip": "Distant meeting", "answer": "fly"},
        schedule=[],
        events=[event],
        now=now,
    )
    assert answer["recorded"] is True


def test_negative_transport_connection_is_unavailable_not_no_booking():
    assert check([flight()], {("ATL airport", "Pickup"): timedelta(seconds=-1)}) is None


def test_negative_home_route_preserves_the_existing_operator_answer(monkeypatch, tmp_path):
    from answer_drive_or_fly import record_answer
    from drive_decision import load_verdicts
    from scan import scan

    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    now = AT - timedelta(days=1)
    event = {
        "id": "meeting",
        "summary": "Distant meeting",
        "location": "Venue",
        "start": {"dateTime": AT.isoformat()},
        "end": {"dateTime": (AT + timedelta(hours=1)).isoformat()},
    }
    meetings = scan([event], now=now, home_address="Home", skip_state={})
    maintain_meeting_routes(
        meetings,
        route=lambda *_: timedelta(hours=4),
        verdicts={},
        booked=set(),
        driving_to={},
        no_routes=set(),
        now=now,
    )
    record_answer(
        {"trip": "Distant meeting", "answer": "fly"}, schedule=[], events=[event], now=now
    )
    previous = load_verdicts(now)
    maintain_meeting_routes(
        meetings,
        route=lambda *_: timedelta(seconds=-1),
        verdicts=previous,
        booked=set(),
        driving_to={},
        no_routes=set(),
        now=now,
    )
    assert load_verdicts(now) == previous
