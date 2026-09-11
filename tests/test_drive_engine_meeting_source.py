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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import build_extended_properties  # noqa: E402
from meeting_source import (  # noqa: E402
    TripPresence,
    exclude_drive_block_events,
    meeting_desired_blocks,
)

UTC = timezone.utc


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
    blocks, skipped = meeting_desired_blocks([m], route=const_route(27))
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
    blocks, _ = meeting_desired_blocks([m], route=const_route(27))
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
    blocks, _ = meeting_desired_blocks([m], route=const_route(20))
    kinds = sorted(b.kind for b in blocks)
    assert kinds == ["meeting_outbound", "meeting_return"]


# --- travel-away suppression (the core fix) ---------------------------------


def test_implausible_drive_is_suppressed():
    # Operator abroad; meeting at home → routed drive is absurd → no block.
    m = FakeMeeting(
        "m1", "Swimming Practice", (FakeLeg("outbound", "Copenhagen", "Pool TN", arrive_by=_dt(9)),)
    )
    blocks, skipped = meeting_desired_blocks([m], route=const_route(9 * 60))  # 9h
    assert blocks == []
    assert any("implausible" in s and "suppressed" in s for s in skipped)


def test_plausible_drive_at_threshold_kept():
    m = FakeMeeting("m1", "Offsite", (FakeLeg("outbound", "Home", "Venue", arrive_by=_dt(9)),))
    blocks, _ = meeting_desired_blocks([m], route=const_route(180))  # exactly 3h
    assert len(blocks) == 1


def test_bridge_drive_longer_than_gap_is_suppressed():
    # A bridge leg whose drive doesn't fit the gap between two meetings can't be
    # made — suppress it (the "5h drive in a 45-min gap" case), don't create it.
    m = FakeMeeting(
        "m1",
        "Second meeting",
        (FakeLeg("bridge", "Venue A", "Venue B", arrive_by=_dt(9), gap_seconds=45 * 60),),
    )
    blocks, skipped = meeting_desired_blocks([m], route=const_route(90))  # 90min > 45min gap
    assert blocks == []
    assert any("exceeds the" in s and "gap" in s for s in skipped)


def test_bridge_drive_within_gap_is_kept():
    m = FakeMeeting(
        "m1",
        "Second meeting",
        (FakeLeg("bridge", "Venue A", "Venue B", arrive_by=_dt(9), gap_seconds=45 * 60),),
    )
    blocks, _ = meeting_desired_blocks([m], route=const_route(20))  # 20min < 45min gap
    assert len(blocks) == 1
    assert blocks[0].kind == "meeting_outbound"


# --- skip paths -------------------------------------------------------------


def test_unresolved_anchor_is_skipped():
    m = FakeMeeting(
        "m1",
        "Meeting",
        (FakeLeg("outbound", None, None, arrive_by=_dt(9), anchor_note="on trip, no lodging yet"),),
    )
    blocks, skipped = meeting_desired_blocks([m], route=const_route(20))
    assert blocks == []
    assert any("no lodging" in s for s in skipped)


def test_route_failure_is_skipped():
    m = FakeMeeting("m1", "Meeting", (FakeLeg("outbound", "Home", "Venue", arrive_by=_dt(9)),))
    blocks, skipped = meeting_desired_blocks([m], route=lambda o, d: None)
    assert blocks == []
    assert any("route failed" in s for s in skipped)


def test_meeting_with_no_legs_yields_nothing():
    m = FakeMeeting("m1", "Virtual standup", ())
    blocks, skipped = meeting_desired_blocks([m], route=const_route(20))
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


def test_exclude_keeps_legacy_dp_blocks_for_scan_has_block():
    # A legacy drive-planner (dp) block must PASS THROUGH: scan uses it to bucket
    # its meeting as already-handled. Dropping it would make the engine create a
    # dengine duplicate on top of the dp block.
    events = [
        {
            "id": "dp1",
            "summary": "Drive: Swimming Practice",
            "description": "x\n[drive-planner:meeting=mtg9:dir=outbound]\n"
            '<!--dp:{"v":2,"a":"2020-07-12T08:00:00+00:00"}-->',
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
        {"id": "m1", "summary": "Real meeting"},
    ]
    kept = [e["id"] for e in exclude_drive_block_events(events)]
    assert "dp1" in kept  # dp kept — scan needs it
    assert "de1" not in kept  # dengine dropped — no self-ingestion
    assert "m1" in kept


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

    blocks, skipped = meeting_desired_blocks([m], route=route)
    assert blocks == []
    assert skipped == ["meeting m1 outbound: origin is the destination — no drive"]
    assert calls == []  # never routed


# --- trips the operator drives to (#242) ------------------------------------


HOME_ADDR = "12 Example St, Sampleton"
LODGING = "611 Historic Nature Trail Gatlinburg"
VENUE = "Rocky Top Sports World"


def _pair_route(pairs):
    return lambda o, d: pairs.get((o, d))


def test_away_suppression_still_fires_for_a_meeting_off_any_trip():
    meeting = FakeMeeting(
        "m1", "Swim practice", (FakeLeg("outbound", HOME_ADDR, VENUE, arrive_by=_dt(20)),)
    )
    blocks, skipped = meeting_desired_blocks([meeting], route=const_route(234))
    assert blocks == []
    assert any("implausible" in note for note in skipped)


def test_away_suppression_spared_for_a_trip_the_operator_drives_to():
    """The cap means "not positioned to drive it — they flew, or are elsewhere".
    On a confirmed drive trip that premise is false, and suppressing deleted the
    event the trip exists for."""
    meeting = FakeMeeting(
        "m1", "Opening Ceremony", (FakeLeg("outbound", HOME_ADDR, VENUE, arrive_by=_dt(20)),)
    )
    blocks, skipped = meeting_desired_blocks(
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
    blocks, _skipped = meeting_desired_blocks(
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
    blocks, _skipped = meeting_desired_blocks(
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
    blocks, _skipped = meeting_desired_blocks(
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
    blocks, skipped = meeting_desired_blocks(
        [meeting],
        route=const_route(5),
        driving_to={"m2": TripPresence(lodging=LODGING, is_first=False, is_last=False)},
    )
    assert blocks == []
    assert skipped == ["meeting m2 outbound: origin is the destination — no drive"]


def test_the_drive_out_to_the_first_event_keeps_home():
    """They really do set off from the house."""
    meeting = FakeMeeting(
        "m1", "Opening Ceremony", (FakeLeg("outbound", HOME_ADDR, VENUE, arrive_by=_dt(20)),)
    )
    routes = _pair_route(
        {(LODGING, VENUE): timedelta(minutes=13), (HOME_ADDR, VENUE): timedelta(minutes=234)}
    )
    blocks, _skipped = meeting_desired_blocks(
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
    blocks, _skipped = meeting_desired_blocks(
        [meeting],
        route=routes,
        driving_to={"m2": TripPresence(lodging=LODGING, is_first=False, is_last=False)},
    )
    assert [(b.origin, b.destination) for b in blocks] == [("Prior Venue", VENUE)]
