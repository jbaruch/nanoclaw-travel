"""Tests for the drive-planner scan classifier (`scan.py`).

Every test maps to a concrete behavior the scan must get right; the
neighbour / idempotency / skip / past tests are named after the LoMBot
`drive_planner` issues whose scars they encode (Epic #59 §5). Fixtures are
built programmatically with the real Google Calendar event *structure*
(timed `dateTime` blocks, `location`, marker-bearing `description`) but
synthetic ids and venues — no live calendar, no real user data.
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-planner"))

from scan import (  # noqa: E402
    DEFAULT_TIGHT_GAP_SECONDS,
    MeetingClass,
    ScanError,
    TransitLeg,
    actionable,
    scan,
)

# Fixed central-time-ish offset so fixtures are deterministic without a
# tzdata dependency; the scan only needs tz-aware datetimes, not a named zone.
CT = timezone(timedelta(hours=-5))
NOW = datetime(2026, 7, 1, 8, 0, tzinfo=CT)
HOME = "1040 Pine Creek Dr, Arrington, TN 37014"


def _timed(start: datetime, end: datetime) -> dict:
    return {
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Chicago"},
    }


def _meeting(
    event_id: str,
    *,
    start: datetime,
    end: datetime,
    location: str | None = "100 Broadway, Nashville, TN",
    summary: str = "Customer sync",
    description: str = "",
) -> dict:
    event = {"id": event_id, "summary": summary, "description": description}
    event.update(_timed(start, end))
    if location is not None:
        event["location"] = location
    return event


def _block(served_id: str, direction: str, *, event_id: str | None = None) -> dict:
    """A planner-created block carrying the self-recognition marker."""
    return _meeting(
        event_id or f"block_{served_id}_{direction}",
        start=NOW + timedelta(hours=1),
        end=NOW + timedelta(hours=2),
        location=HOME,
        summary="\U0001f697 Drive",
        description=f"[drive-planner:meeting={served_id}:dir={direction}]",
    )


def _by_id(results: list[MeetingClass]) -> dict[str, MeetingClass]:
    return {r.meeting_id: r for r in results}


# --- baseline -------------------------------------------------------------


def test_standalone_meeting_is_needs_decision_with_both_legs():
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=1)
    [result] = scan([_meeting("m1", start=start, end=end)], now=NOW, home_address=HOME)

    assert result.bucket == "needs_decision"
    directions = [leg.direction for leg in result.legs]
    assert directions == ["outbound", "return"]
    outbound, ret = result.legs
    assert outbound.origin == HOME
    assert outbound.arrive_by == start
    assert ret.destination == HOME
    assert ret.depart_after == end


def test_nothing_is_silently_dropped():
    events = [
        _meeting("m1", start=NOW + timedelta(hours=3), end=NOW + timedelta(hours=4)),
        _meeting("allday", start=NOW, end=NOW, location="X"),
        _block("m1", "outbound"),
    ]
    events[1].pop("start")
    events[1]["start"] = {"date": "2026-07-01"}
    events[1]["end"] = {"date": "2026-07-02"}
    results = scan(events, now=NOW, home_address=HOME)
    assert len(results) == len(events)


# --- lombot #50: ANY marker = handled, idempotent -------------------------


def test_lombot50_any_marker_makes_meeting_has_block():
    start = NOW + timedelta(hours=3)
    events = [
        _meeting("m1", start=start, end=start + timedelta(hours=1)),
        _block("m1", "outbound"),  # outbound ONLY — not both directions
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert by_id["m1"].bucket == "has_block"
    assert by_id["m1"].present_directions == ("outbound",)
    assert by_id["m1"].legs == ()


def test_lombot50_present_directions_dedup_both_legs():
    start = NOW + timedelta(hours=3)
    events = [
        _meeting("m1", start=start, end=start + timedelta(hours=1)),
        _block("m1", "outbound", event_id="b1"),
        _block("m1", "return", event_id="b2"),
        _block("m1", "outbound", event_id="b3"),  # duplicate direction
    ]
    result = _by_id(scan(events, now=NOW, home_address=HOME))["m1"]
    assert result.bucket == "has_block"
    assert set(result.present_directions) == {"outbound", "return"}
    assert len(result.present_directions) == 2  # deduped


def test_planner_block_itself_is_filtered():
    result = scan([_block("m1", "outbound")], now=NOW, home_address=HOME)[0]
    assert result.bucket == "filtered"
    assert result.reason == "planner block"


# --- lombot #49: skips persist with expiry; virtual never asked ----------


def test_lombot49_active_skip_is_skipped():
    start = NOW + timedelta(hours=3)
    skip = {"m1": (NOW + timedelta(days=2)).isoformat()}
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
        skip_state=skip,
    )[0]
    assert result.bucket == "skipped"


def test_lombot49_expired_skip_reverts_to_needs_decision():
    start = NOW + timedelta(hours=3)
    skip = {"m1": (NOW - timedelta(days=1)).isoformat()}
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
        skip_state=skip,
    )[0]
    assert result.bucket == "needs_decision"


def test_lombot49_malformed_skip_expiry_reverts_to_needs_decision():
    start = NOW + timedelta(hours=3)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
        skip_state={"m1": "not-a-date"},
    )[0]
    assert result.bucket == "needs_decision"


@pytest.mark.parametrize(
    "location",
    [
        "https://zoom.us/j/123",
        "meet.google.com/abc-defg-hij",
        "Microsoft Teams Meeting (teams.microsoft.com/l/x)",
        "Online",
        "Phone call",
    ],
)
def test_lombot49_virtual_locations_are_filtered(location):
    start = NOW + timedelta(hours=3)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1), location=location)],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "filtered"
    assert result.reason == "virtual location"


def test_missing_location_is_filtered():
    start = NOW + timedelta(hours=3)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1), location=None)],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "filtered"
    assert result.reason == "no location"


def test_all_day_event_is_filtered():
    event = {
        "id": "m1",
        "summary": "Conference",
        "location": "Austin, TX",
        "start": {"date": "2026-07-02"},
        "end": {"date": "2026-07-03"},
    }
    result = scan([event], now=NOW, home_address=HOME)[0]
    assert result.bucket == "filtered"
    assert result.reason == "all-day event"


# --- lombot #28: past guard ----------------------------------------------


def test_lombot28_past_meeting_is_bucketed_past():
    start = NOW - timedelta(hours=2)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "past"
    assert result.legs == ()


def test_lombot28_just_started_within_tolerance_is_not_past():
    start = NOW - timedelta(minutes=2)  # inside PAST_TOLERANCE
    result = scan(
        [_meeting("m1", start=start, end=NOW + timedelta(minutes=58))],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "needs_decision"


# --- lombot #37: multiline location normalized ---------------------------


def test_lombot37_multiline_location_is_whitespace_normalized():
    start = NOW + timedelta(hours=3)
    messy = "Acme HQ\n  500 Main St\tSuite 4\nNashville,  TN"
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1), location=messy)],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.location == "Acme HQ 500 Main St Suite 4 Nashville, TN"


# --- lombot #14/#7: neighbour-aware (same vs different venue, tight gap) --


def test_lombot14_same_venue_tight_gap_is_back_to_back():
    venue = "100 Broadway, Nashville, TN"
    a_start = NOW + timedelta(hours=3)
    a_end = a_start + timedelta(hours=1)
    b_start = a_end + timedelta(minutes=15)  # tight
    b_end = b_start + timedelta(hours=1)
    events = [
        _meeting("a", start=a_start, end=a_end, location=venue),
        _meeting("b", start=b_start, end=b_end, location=venue),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    # First of the same-venue run: outbound from home, NO return (you stay).
    assert [leg.direction for leg in by_id["a"].legs] == ["outbound"]
    # Second: no inbound leg (already there), return home. It is back_to_back.
    assert by_id["b"].bucket == "back_to_back"
    assert [leg.direction for leg in by_id["b"].legs] == ["return"]


def test_lombot7_different_venue_tight_gap_is_bridge_with_gap_exposed():
    a_start = NOW + timedelta(hours=3)
    a_end = a_start + timedelta(hours=1)
    b_start = a_end + timedelta(minutes=30)  # tight, different venue
    b_end = b_start + timedelta(hours=1)
    events = [
        _meeting("a", start=a_start, end=a_end, location="100 Broadway, Nashville"),
        _meeting("b", start=b_start, end=b_end, location="900 Division St, Nashville"),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert by_id["b"].bucket == "bridge"
    bridge_legs = [leg for leg in by_id["b"].legs if leg.direction == "bridge"]
    assert len(bridge_legs) == 1
    bridge = bridge_legs[0]
    assert bridge.origin == "100 Broadway, Nashville"
    assert bridge.destination == "900 Division St, Nashville"
    # gap exposed so the router can warn when drive_time > gap (lombot #14/#7)
    assert bridge.gap_seconds == 30 * 60
    assert bridge.arrive_by == b_start


def test_large_gap_same_day_keeps_both_independent():
    a_start = NOW + timedelta(hours=3)
    a_end = a_start + timedelta(hours=1)
    b_start = a_end + timedelta(hours=4)  # well over the tight threshold
    b_end = b_start + timedelta(hours=1)
    events = [
        _meeting("a", start=a_start, end=a_end, location="100 Broadway"),
        _meeting("b", start=b_start, end=b_end, location="900 Division St"),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert by_id["a"].bucket == "needs_decision"
    assert [leg.direction for leg in by_id["a"].legs] == ["outbound", "return"]
    assert by_id["b"].bucket == "needs_decision"
    assert [leg.direction for leg in by_id["b"].legs] == ["outbound", "return"]


def test_three_same_venue_run_anchors_outbound_first_return_last():
    venue = "100 Broadway, Nashville, TN"
    starts = [NOW + timedelta(hours=3, minutes=90 * i) for i in range(3)]
    events = [
        _meeting(f"m{i}", start=s, end=s + timedelta(minutes=30), location=venue)
        for i, s in enumerate(starts)
    ]
    # 30-min meetings, 60-min gaps → tight (≤ 90 min) and same venue.
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert [leg.direction for leg in by_id["m0"].legs] == ["outbound"]
    assert by_id["m1"].bucket == "back_to_back"
    assert by_id["m1"].legs == ()  # middle of the run: no transit at all
    assert [leg.direction for leg in by_id["m2"].legs] == ["return"]


def test_tight_gap_threshold_is_configurable():
    a_start = NOW + timedelta(hours=3)
    a_end = a_start + timedelta(hours=1)
    b_start = a_end + timedelta(minutes=40)
    b_end = b_start + timedelta(hours=1)
    events = [
        _meeting("a", start=a_start, end=a_end, location="100 Broadway"),
        _meeting("b", start=b_start, end=b_end, location="900 Division St"),
    ]
    # With a 30-min threshold, a 40-min gap is NOT tight → independent trips.
    by_id = _by_id(scan(events, now=NOW, home_address=HOME, tight_gap_seconds=30 * 60))
    assert by_id["b"].bucket == "needs_decision"


# --- helpers and input guards --------------------------------------------


def test_actionable_filters_to_action_buckets():
    start = NOW + timedelta(hours=3)
    events = [
        _meeting("decide", start=start, end=start + timedelta(hours=1)),
        _block("decide2", "outbound"),
    ]
    events.append(_meeting("decide2", start=start, end=start + timedelta(hours=1)))
    results = scan(events, now=NOW, home_address=HOME)
    act = actionable(results)
    assert {r.meeting_id for r in act} == {"decide"}


def test_naive_now_raises_scan_error():
    with pytest.raises(ScanError, match="timezone-naive"):
        scan([], now=datetime(2026, 7, 1, 8, 0), home_address=HOME)


def test_empty_home_address_raises_scan_error():
    with pytest.raises(ScanError, match="home_address"):
        scan([], now=NOW, home_address="")


def test_events_must_be_a_list():
    with pytest.raises(ScanError, match="must be a list"):
        scan({"id": "m1"}, now=NOW, home_address=HOME)  # type: ignore[arg-type]


def test_default_threshold_constant_is_ninety_minutes():
    assert DEFAULT_TIGHT_GAP_SECONDS == 90 * 60


def test_transit_leg_is_frozen():
    leg = TransitLeg(direction="outbound", origin=HOME, destination="X")
    with pytest.raises(FrozenInstanceError):
        leg.direction = "return"  # type: ignore[misc]
