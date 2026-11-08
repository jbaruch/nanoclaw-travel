"""Tests for the meeting-leg source (scan results → unified DesiredBlocks).

Deterministic fixtures only — hand-built meeting/leg stand-ins (duck-typed to the
scan MeetingClass/TransitLeg surface), a fake router, fixed datetimes. These pin
the two behaviors that make meeting drives correct: an implausibly long routed
drive is suppressed (the operator is away), and each block carries the meeting's
local timezone. Unresolved anchors and route failures skip with diagnostics.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import build_extended_properties  # noqa: E402
from meeting_source import (  # noqa: E402
    TripPresence,
    exclude_drive_block_events,
    meeting_desired_blocks,
)

UTC = UTC


def _dt(h, mi=0, *, day=13):
    return datetime(2020, 7, day, h, mi, tzinfo=UTC)


@dataclass
class FakeLeg:
    direction: str
    origin: str | None
    destination: str | None
    arrive_by: datetime | None = None
    depart_after: datetime | None = None
    anchor_note: str | None = None
    gap_seconds: int | None = None


@dataclass
class FakeMeeting:
    meeting_id: str
    summary: str
    legs: tuple
    timezone: str | None = "America/Chicago"


def const_route(minutes):
    return lambda o, d: timedelta(minutes=minutes)


# --- outbound / return legs -------------------------------------------------


def test_outbound_leg_builds_arrive_by_anchored_block():
    m = FakeMeeting(
        "m1",
        "Swimming Practice",
        (FakeLeg("outbound", "Home", "Pool", arrive_by=_dt(9, 0)),),
    )
    blocks, skipped, _held = meeting_desired_blocks([m], route=const_route(27))
    assert skipped == []
    assert len(blocks) == 1
    b = blocks[0]
    assert b.kind == "meeting_outbound"
    assert b.identity == "m1"
    assert b.summary == "Drive: Swimming Practice"
    assert b.end == _dt(9, 0)  # arrive by meeting start
    assert b.start == _dt(9, 0) - timedelta(minutes=27)
    assert b.origin == "Home" and b.destination == "Pool"
    assert b.timezone == "America/Chicago"


def test_return_leg_builds_depart_after_anchored_block():
    m = FakeMeeting(
        "m1", "Swimming Practice", (FakeLeg("return", "Pool", "Home", depart_after=_dt(11, 0)),)
    )
    blocks, _, _held = meeting_desired_blocks([m], route=const_route(27))
    b = blocks[0]
    assert b.kind == "meeting_return"
    assert b.start == _dt(11, 0)
    assert b.end == _dt(11, 0) + timedelta(minutes=27)


def test_outbound_and_return_are_distinct_blocks():
    m = FakeMeeting(
        "m1",
        "Practice",
        (
            FakeLeg("outbound", "Home", "Pool", arrive_by=_dt(9)),
            FakeLeg("return", "Pool", "Home", depart_after=_dt(11)),
        ),
    )
    blocks, _, _held = meeting_desired_blocks([m], route=const_route(20))
    kinds = sorted(b.kind for b in blocks)
    assert kinds == ["meeting_outbound", "meeting_return"]


# --- travel-away suppression (the core fix) ---------------------------------


def test_implausible_drive_is_suppressed():
    # Operator abroad; meeting at home → routed drive is absurd → no block.
    m = FakeMeeting(
        "m1", "Swimming Practice", (FakeLeg("outbound", "Copenhagen", "Pool TN", arrive_by=_dt(9)),)
    )
    blocks, skipped, _held = meeting_desired_blocks([m], route=const_route(9 * 60))  # 9h
    assert blocks == []
    assert any("implausible" in s and "suppressed" in s for s in skipped)


def test_plausible_drive_at_threshold_kept():
    m = FakeMeeting("m1", "Offsite", (FakeLeg("outbound", "Home", "Venue", arrive_by=_dt(9)),))
    blocks, _, _held = meeting_desired_blocks([m], route=const_route(180))  # exactly 3h
    assert len(blocks) == 1


def test_bridge_drive_longer_than_gap_is_suppressed():
    # A bridge leg whose drive doesn't fit the gap between two meetings can't be
    # made — suppress it (the "5h drive in a 45-min gap" case), don't create it.
    m = FakeMeeting(
        "m1",
        "Second meeting",
        (FakeLeg("bridge", "Venue A", "Venue B", arrive_by=_dt(9), gap_seconds=45 * 60),),
    )
    blocks, skipped, _held = meeting_desired_blocks([m], route=const_route(90))  # 90min > 45min gap
    assert blocks == []
    assert any("exceeds the" in s and "gap" in s for s in skipped)


def test_bridge_drive_within_gap_is_kept():
    m = FakeMeeting(
        "m1",
        "Second meeting",
        (FakeLeg("bridge", "Venue A", "Venue B", arrive_by=_dt(9), gap_seconds=45 * 60),),
    )
    blocks, _, _held = meeting_desired_blocks([m], route=const_route(20))  # 20min < 45min gap
    assert len(blocks) == 1
    assert blocks[0].kind == "meeting_outbound"


# --- skip paths -------------------------------------------------------------


def test_unresolved_anchor_is_skipped():
    m = FakeMeeting(
        "m1",
        "Meeting",
        (FakeLeg("outbound", None, None, arrive_by=_dt(9), anchor_note="on trip, no lodging yet"),),
    )
    blocks, skipped, _held = meeting_desired_blocks([m], route=const_route(20))
    assert blocks == []
    assert any("no lodging" in s for s in skipped)


def test_route_failure_is_skipped():
    m = FakeMeeting("m1", "Meeting", (FakeLeg("outbound", "Home", "Venue", arrive_by=_dt(9)),))
    blocks, skipped, _held = meeting_desired_blocks([m], route=lambda o, d: None)
    assert blocks == []
    assert any("route failed" in s for s in skipped)


def test_meeting_with_no_legs_yields_nothing():
    m = FakeMeeting("m1", "Virtual standup", ())
    blocks, skipped, _held = meeting_desired_blocks([m], route=const_route(20))
    assert blocks == [] and skipped == []


# --- self-ingestion guard: drop the engine's own Drive: blocks from scan input ---


def test_exclude_drive_block_events_by_summary_prefix():
    events = [
        {"id": "e1", "summary": "Swimming Practice", "location": "Pool"},
        {"id": "d1", "summary": "Drive: Swimming Practice", "location": "Pool"},
        {"id": "e2", "summary": "Dentist"},
    ]
    kept = exclude_drive_block_events(events)
    assert [e["id"] for e in kept] == ["e1", "e2"]  # the Drive: block is dropped


def test_exclude_drive_block_events_by_marker():
    # A drive block recognized by its codec state is dropped even if some tool
    # renamed the summary — no self-referential re-ingestion.
    events = [
        {
            "id": "d1",
            "summary": "renamed somehow",
            "extendedProperties": build_extended_properties(
                identity="BNA-JFK-20200712T0900Z",
                kind="airport_departure",
                baseline_seconds=600,
                anchor=_dt(8, 0),
                origin="Home",
                destination="BNA",
            ),
        },
        {"id": "e1", "summary": "Real meeting"},
    ]
    kept = exclude_drive_block_events(events)
    assert [e["id"] for e in kept] == ["e1"]


def test_exclude_keeps_retired_drive_planner_blocks_for_scan_has_block():
    # A block the retired drive-planner left must PASS THROUGH: scan reads its
    # marker to bucket the meeting as already handled. Dropping it would make
    # the engine create a duplicate block on top of it.
    events = [
        {
            "id": "dp1",
            "summary": "Drive: Swimming Practice",
            "description": "x\n[drive-planner:meeting=mtg9:dir=outbound]\n",
        },
        {
            "id": "de1",
            "summary": "Drive: Football",
            "extendedProperties": build_extended_properties(
                identity="mtg8",
                kind="meeting_outbound",
                baseline_seconds=600,
                anchor=_dt(8, 0),
                origin="Home",
                destination="Venue",
            ),
        },
        {"id": "x1", "summary": "Drive: no marker at all"},
        {"id": "m1", "summary": "Real meeting"},
    ]
    assert [e["id"] for e in exclude_drive_block_events(events)] == ["dp1", "m1"]


# --- display zone (#301) ----------------------------------------------------


def test_display_tz_overrides_the_meeting_zone():
    """A source event with a stale `+02:00` offset on an `America/Chicago`
    event scans to `Etc/GMT-2`; the block must render on the operator's clock."""
    m = FakeMeeting(
        "m1",
        "Baruch 1:1",
        (FakeLeg("outbound", "Home", "1004 Nelson Merry St", arrive_by=_dt(19, 0)),),
        timezone="Etc/GMT-2",
    )
    blocks, skipped, _held = meeting_desired_blocks(
        [m], route=const_route(38), display_tz="America/Chicago"
    )
    assert skipped == []
    assert [b.timezone for b in blocks] == ["America/Chicago"]
    assert blocks[0].end == _dt(19, 0)  # the instant is untouched


def test_no_display_tz_keeps_the_meeting_zone():
    m = FakeMeeting(
        "m1",
        "Baruch 1:1",
        (FakeLeg("outbound", "Home", "1004 Nelson Merry St", arrive_by=_dt(19, 0)),),
        timezone="Etc/GMT-2",
    )
    blocks, _, _held = meeting_desired_blocks([m], route=const_route(38), display_tz=None)
    assert [b.timezone for b in blocks] == ["Etc/GMT-2"]
    assert [b.zone_from_operator for b in blocks] == [False]


def test_display_tz_marks_the_block_zone_as_the_operators():
    """#309: only an operator-supplied zone may re-patch an existing block."""
    m = FakeMeeting(
        "m1",
        "Baruch 1:1",
        (FakeLeg("outbound", "Home", "1004 Nelson Merry St", arrive_by=_dt(19, 0)),),
        timezone="Etc/GMT-2",
    )
    blocks, _, _held = meeting_desired_blocks(
        [m], route=const_route(38), display_tz="America/Chicago"
    )
    assert [b.zone_from_operator for b in blocks] == [True]


# --- a leg whose origin is its destination (#301) ---------------------------


def test_leg_from_the_anchor_to_the_anchor_is_skipped():
    """A home→home leg is a zero-length drive that still lands as a degenerate
    one-minute block. No block, one diagnostic."""
    m = FakeMeeting(
        "m1",
        "ExamOne Appointment",
        (FakeLeg("outbound", "Home", "Home", arrive_by=_dt(15, 0)),),
    )
    calls: list[tuple[str, str]] = []

    def route(o, d):
        calls.append((o, d))
        return timedelta(0)

    blocks, skipped, _held = meeting_desired_blocks([m], route=route)
    assert blocks == []
    assert skipped == ["meeting m1 outbound: origin is the destination — no drive"]
    assert calls == []  # never routed


# --- trips the operator drives to (#242) ------------------------------------


HOME_ADDR = "12 Example St, Sampleton"
LODGING = "611 Historic Nature Trail Gatlinburg"
VENUE = "Rocky Top Sports World"


def _pair_route(pairs):
    return lambda o, d: pairs.get((o, d))


def test_long_drive_from_home_without_a_trip_is_planned():
    meeting = FakeMeeting(
        "m1", "Swim practice", (FakeLeg("outbound", HOME_ADDR, VENUE, arrive_by=_dt(20)),)
    )
    blocks, skipped, _held = meeting_desired_blocks([meeting], route=const_route(234))
    assert [(b.origin, b.destination, b.baseline_seconds) for b in blocks] == [
        (HOME_ADDR, VENUE, 234 * 60)
    ]
    assert blocks[0].assumed_drive is True
    assert skipped == []


def test_asher_pickup_gets_both_real_drives():
    meeting = FakeMeeting(
        "pickup",
        "Asher pickup",
        (
            FakeLeg("outbound", HOME_ADDR, "ATL parking", arrive_by=_dt(16)),
            FakeLeg("return", "ATL parking", HOME_ADDR, depart_after=_dt(17)),
        ),
    )
    blocks, skipped, held = meeting_desired_blocks([meeting], route=const_route(227))
    assert [(b.kind, b.start, b.end) for b in blocks] == [
        ("meeting_outbound", _dt(12, 13), _dt(16)),
        ("meeting_return", _dt(17), _dt(20, 47)),
    ]
    assert skipped == held == []


def test_ordinary_drive_does_not_check_transport_or_assume_a_road_trip():
    def unexpected_lookup(_):
        pytest.fail("ordinary meeting drives must not look for flights or trains")

    meeting = FakeMeeting(
        "dentist", "Dentist", (FakeLeg("outbound", HOME_ADDR, "Office", arrive_by=_dt(16)),)
    )
    blocks, _, _ = meeting_desired_blocks(
        [meeting],
        route=const_route(180),
        booked_transport=unexpected_lookup,
    )
    assert len(blocks) == 1
    assert blocks[0].assumed_drive is False


def test_fly_answer_removes_the_long_drive_without_holding_it():
    meeting = FakeMeeting(
        "pickup", "Pickup", (FakeLeg("outbound", HOME_ADDR, "ATL", arrive_by=_dt(16)),)
    )
    blocks, _, held = meeting_desired_blocks(
        [meeting],
        route=const_route(227),
        operator_answers={"pickup": "fly"},
        booked_transport=lambda _: None,
    )
    assert blocks == held == []


def test_unknown_booking_destination_does_not_become_an_assumed_drive():
    meeting = FakeMeeting(
        "pickup", "Pickup", (FakeLeg("outbound", HOME_ADDR, "ATL", arrive_by=_dt(16)),)
    )
    blocks, skipped, held = meeting_desired_blocks(
        [meeting],
        route=const_route(227),
        booked_transport=lambda _: None,
    )
    assert blocks == []
    assert len(held) == 1
    assert "transport destination unavailable" in skipped[0]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("transport", ["fly", "booking"])
def test_transport_replaces_both_legs_of_an_asymmetric_long_meeting(reverse, transport):
    legs = [
        FakeLeg("outbound", HOME_ADDR, "ATL", arrive_by=_dt(16)),
        FakeLeg("return", "ATL", HOME_ADDR, depart_after=_dt(17)),
    ]
    meeting = FakeMeeting("pickup", "Pickup", tuple(reversed(legs)) if reverse else tuple(legs))
    blocks, skipped, held = meeting_desired_blocks(
        [meeting],
        route=lambda a, b: timedelta(minutes=240 if a == HOME_ADDR else 150),
        operator_answers={"pickup": "fly"} if transport == "fly" else {},
        booked_transport=lambda _: transport == "booking",
    )
    assert blocks == held == []
    assert any("operator is flying" in line or "booked transport" in line for line in skipped)


def test_an_explicit_drive_answer_is_not_announced_as_an_assumption():
    meeting = FakeMeeting(
        "pickup", "Pickup", (FakeLeg("outbound", HOME_ADDR, "ATL", arrive_by=_dt(16)),)
    )
    blocks, _, held = meeting_desired_blocks(
        [meeting],
        route=const_route(227),
        operator_answers={"pickup": "drive"},
        booked_transport=lambda _: None,
    )
    assert len(blocks) == 1 and held == []
    assert blocks[0].assumed_drive is False


def test_booking_replaces_even_an_assumed_drive_with_an_unresolved_new_anchor():
    meeting = FakeMeeting(
        "pickup", "Pickup", (FakeLeg("outbound", None, "ATL", arrive_by=_dt(16)),)
    )
    blocks, skipped, held = meeting_desired_blocks(
        [meeting],
        route=const_route(227),
        booked_transport=lambda _: True,
    )
    assert blocks == held == []
    assert "booked transport" in skipped[0]


def test_away_suppression_spared_for_a_trip_the_operator_drives_to():
    """The cap means "not positioned to drive it — they flew, or are elsewhere".
    On a confirmed drive trip that premise is false, and suppressing deleted the
    event the trip exists for."""
    meeting = FakeMeeting(
        "m1", "Opening Ceremony", (FakeLeg("outbound", HOME_ADDR, VENUE, arrive_by=_dt(20)),)
    )
    blocks, skipped, _held = meeting_desired_blocks(
        [meeting],
        route=const_route(234),
        driving_to={"m1": TripPresence(lodging=LODGING, is_first=True, is_last=False)},
    )
    assert [b.origin for b in blocks] == [HOME_ADDR]
    assert [b.destination for b in blocks] == [VENUE]
    assert skipped == []


def test_the_drive_back_from_a_mid_trip_event_goes_to_the_lodging_not_home():
    """`scan` resolves one anchor per meeting at the event's start, so a check-in
    stamped after the first event made that event's return leg drive home — out
    of the middle of a trip."""
    meeting = FakeMeeting(
        "m1", "Opening Ceremony", (FakeLeg("return", VENUE, HOME_ADDR, depart_after=_dt(22)),)
    )
    routes = _pair_route(
        {(VENUE, LODGING): timedelta(minutes=13), (VENUE, HOME_ADDR): timedelta(minutes=234)}
    )
    blocks, _skipped, _held = meeting_desired_blocks(
        [meeting],
        route=routes,
        driving_to={"m1": TripPresence(lodging=LODGING, is_first=True, is_last=False)},
    )
    assert [(b.origin, b.destination) for b in blocks] == [(VENUE, LODGING)]
    assert blocks[0].end - blocks[0].start == timedelta(minutes=13)


def test_the_drive_back_from_the_last_event_keeps_home():
    """`lodging_source` owns the drive home; this leg deferring to it keeps one
    owner for the way back."""
    meeting = FakeMeeting(
        "m1", "Final game", (FakeLeg("return", VENUE, HOME_ADDR, depart_after=_dt(22)),)
    )
    routes = _pair_route(
        {(VENUE, LODGING): timedelta(minutes=13), (VENUE, HOME_ADDR): timedelta(minutes=234)}
    )
    blocks, _skipped, _held = meeting_desired_blocks(
        [meeting],
        route=routes,
        driving_to={"m1": TripPresence(lodging=LODGING, is_first=False, is_last=True)},
    )
    assert [(b.origin, b.destination) for b in blocks] == [(VENUE, HOME_ADDR)]


def test_the_drive_out_to_a_later_event_starts_from_the_lodging():
    meeting = FakeMeeting("m2", "Game", (FakeLeg("outbound", HOME_ADDR, VENUE, arrive_by=_dt(18)),))
    routes = _pair_route(
        {(LODGING, VENUE): timedelta(minutes=13), (HOME_ADDR, VENUE): timedelta(minutes=234)}
    )
    blocks, _skipped, _held = meeting_desired_blocks(
        [meeting],
        route=routes,
        driving_to={"m2": TripPresence(lodging=LODGING, is_first=False, is_last=True)},
    )
    assert [(b.origin, b.destination) for b in blocks] == [(LODGING, VENUE)]


def test_a_venue_at_the_lodging_collapses_after_the_presence_rewrite():
    """Mid-trip, the outbound origin is rewritten home→lodging; a meeting held
    AT the lodging then reads lodging→lodging and is skipped, not routed."""
    meeting = FakeMeeting(
        "m2", "Hotel breakfast talk", (FakeLeg("outbound", HOME_ADDR, LODGING, arrive_by=_dt(8)),)
    )
    blocks, skipped, _held = meeting_desired_blocks(
        [meeting],
        route=const_route(5),
        driving_to={"m2": TripPresence(lodging=LODGING, is_first=False, is_last=False)},
    )
    assert blocks == []
    assert skipped == ["meeting m2 outbound: origin is the destination — no drive"]


def test_a_venue_at_the_lodging_collapses_despite_formatting_differences():
    """Copilot on #308: the schedule's lodging is only stripped while the
    scan's location is whitespace-collapsed; a line break or a case change
    between the two must still read as the same place."""
    lodging = "Grand Hotel,\n  1 Main St, Sampleton"
    meeting = FakeMeeting(
        "m3",
        "Hotel breakfast talk",
        (FakeLeg("outbound", HOME_ADDR, "grand hotel, 1 main st, sampleton", arrive_by=_dt(8)),),
    )
    blocks, skipped, _held = meeting_desired_blocks(
        [meeting],
        route=const_route(5),
        driving_to={"m3": TripPresence(lodging=lodging, is_first=False, is_last=False)},
    )
    assert blocks == []
    assert skipped == ["meeting m3 outbound: origin is the destination — no drive"]


def test_the_drive_out_to_the_first_event_keeps_home():
    """They really do set off from the house."""
    meeting = FakeMeeting(
        "m1", "Opening Ceremony", (FakeLeg("outbound", HOME_ADDR, VENUE, arrive_by=_dt(20)),)
    )
    routes = _pair_route(
        {(LODGING, VENUE): timedelta(minutes=13), (HOME_ADDR, VENUE): timedelta(minutes=234)}
    )
    blocks, _skipped, _held = meeting_desired_blocks(
        [meeting],
        route=routes,
        driving_to={"m1": TripPresence(lodging=LODGING, is_first=True, is_last=False)},
    )
    assert [(b.origin, b.destination) for b in blocks] == [(HOME_ADDR, VENUE)]


def test_a_bridge_leg_keeps_its_prior_venue_origin_on_a_driving_trip():
    """A bridge leg runs venue→venue between two tight-gap meetings; its origin
    is the prior venue, not the anchor. Rewriting it to the lodging invents a
    detour through the hotel between back-to-back events."""
    meeting = FakeMeeting(
        "m2",
        "Second session",
        (FakeLeg("bridge", "Prior Venue", VENUE, arrive_by=_dt(15), gap_seconds=3600),),
    )
    routes = _pair_route(
        {("Prior Venue", VENUE): timedelta(minutes=20), (LODGING, VENUE): timedelta(minutes=13)}
    )
    blocks, _skipped, _held = meeting_desired_blocks(
        [meeting],
        route=routes,
        driving_to={"m2": TripPresence(lodging=LODGING, is_first=False, is_last=False)},
    )
    assert [(b.origin, b.destination) for b in blocks] == [("Prior Venue", VENUE)]
