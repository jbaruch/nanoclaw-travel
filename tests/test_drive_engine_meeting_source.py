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

from meeting_source import exclude_drive_block_events, meeting_desired_blocks  # noqa: E402

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
    # A drive block recognized by its codec marker is dropped even if some tool
    # renamed the summary — no self-referential re-ingestion.
    events = [
        {
            "id": "d1",
            "summary": "renamed somehow",
            "description": "x\n[drive-engine:leg=BNA-JFK-20200712T0900Z:kind=airport_departure]\n"
            '<!--dengine:{"schema_version":1,"a":"2020-07-12T08:00:00+00:00"}-->',
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
            "description": "y\n[drive-engine:leg=mtg8:kind=meeting_outbound]\n"
            '<!--dengine:{"schema_version":1,"a":"2020-07-12T08:00:00+00:00"}-->',
        },
        {"id": "m1", "summary": "Real meeting"},
    ]
    kept = [e["id"] for e in exclude_drive_block_events(events)]
    assert "dp1" in kept  # dp kept — scan needs it
    assert "de1" not in kept  # dengine dropped — no self-ingestion
    assert "m1" in kept
