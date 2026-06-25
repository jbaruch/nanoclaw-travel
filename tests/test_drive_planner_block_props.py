"""Tests for the drive-planner block-props codec (`block_props.py`).

Covers the round trip the calendar-as-state design depends on: build the
create-event arguments for a block, then parse a fetched event carrying that
same description back into a `BlockState`. State lives in the event
**description** (the live Composio v3 toolkit has no writable
extendedProperties), so the codec encodes a `<!--dp:{...}-->` JSON comment plus
the `scan` marker, and parses both back. Pins two contracts that, if they
drift, break silently in production:

  * the marker token must match `scan._MARKER_RE` (lombot #50 — duplicate
    blocks if scan stops recognizing them);
  * a malformed / non-block event parses to None rather than raising, so one
    bad event can never abort the recheck poll.

Synthetic fixtures only — no live calendar, no real keys.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-planner"))

from block_props import (  # noqa: E402
    ALERT_GROWTH,
    ALERT_LEAVE_NOW,
    BLOCK_SCHEMA_VERSION,
    BlockState,
    build_block_args,
    build_description,
    build_marker,
    next_alerts,
    parse_alerted,
    parse_block,
    parse_marker,
    serialize_alerted,
)
from scan import _MARKER_RE  # noqa: E402

CT = timezone(timedelta(hours=-5))
ARRIVE = datetime(2026, 7, 2, 13, 0, tzinfo=CT)
LEG_START = datetime(2026, 7, 2, 12, 30, tzinfo=CT)


def _build_args(**overrides):
    args = {
        "calendar_id": "primary",
        "meeting_id": "evt_42",
        "direction": "outbound",
        "summary": "Drive to Customer sync",
        "leg_start": LEG_START,
        "arrive_by": ARRIVE,
        "baseline_seconds": 1500,
        "origin": "12 Example St, Sampleton, TN 37000",
        "destination": "100 Broadway, Nashville, TN",
    }
    args.update(overrides)
    return build_block_args(**args)


def _event(*, description: str, event_id: str = "block_1", summary: str = "Drive to Customer sync"):
    """A fetched event carrying a drive-planner description."""
    return {"id": event_id, "summary": summary, "description": description}


def _event_from_args(args: dict, *, event_id: str = "block_1") -> dict:
    return _event(description=args["description"], event_id=event_id, summary=args["summary"])


# --- marker contract with scan.py ----------------------------------------


def test_built_marker_matches_scan_regex():
    match = _MARKER_RE.search(build_marker("evt_42", "outbound"))
    assert match is not None and match["id"] == "evt_42" and match["dir"] == "outbound"


def test_parse_marker_round_trips():
    assert parse_marker(build_marker("evt_9", "bridge")) == ("evt_9", "bridge")
    assert parse_marker("no marker here") is None


def test_description_carries_scan_marker():
    match = _MARKER_RE.search(_build_args()["description"])
    assert match is not None and match["id"] == "evt_42" and match["dir"] == "outbound"


# --- create-arg shape (live v3 contract) ---------------------------------


def test_block_is_free_by_default():
    assert _build_args()["transparency"] == "transparent"


def test_busy_block_is_opaque():
    assert _build_args(busy=True)["transparency"] == "opaque"


def test_create_args_use_flat_start_and_duration():
    args = _build_args()
    # flat start_datetime, no nested start/end
    assert args["start_datetime"] == LEG_START.isoformat()
    assert "start" not in args and "end" not in args
    # 12:30 -> 13:00 is 30 minutes
    assert args["event_duration_hour"] == 0
    assert args["event_duration_minutes"] == 30
    # destination on the location field, no extendedProperties
    assert args["location"] == "100 Broadway, Nashville, TN"
    assert "extendedProperties" not in args


def test_description_state_round_trips_via_parse_block():
    state = parse_block(_event_from_args(_build_args()))
    assert isinstance(state, BlockState)
    assert state.meeting_id == "evt_42"
    assert state.direction == "outbound"
    assert state.baseline_seconds == 1500
    assert state.arrive_by == ARRIVE
    assert state.origin.startswith("12 Example St")
    assert state.destination == "100 Broadway, Nashville, TN"
    assert state.summary == "Drive to Customer sync"
    assert state.alerted == frozenset()


def test_schema_version_in_state():
    args = _build_args()
    assert f'"v":{BLOCK_SCHEMA_VERSION}' in args["description"]


# --- input guards --------------------------------------------------------


def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        _build_args(arrive_by=datetime(2026, 7, 2, 13, 0))


def test_unknown_direction_rejected():
    with pytest.raises(ValueError, match="outbound/return/bridge"):
        _build_args(direction="sideways")


def test_empty_endpoint_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        _build_args(origin="")


# --- parse rejections ----------------------------------------------------


def test_parse_non_block_event_returns_none():
    assert parse_block({"id": "evt_1", "summary": "Customer sync", "description": "plain"}) is None


def test_parse_non_dict_returns_none():
    assert parse_block("garbage") is None
    assert parse_block(None) is None


def test_parse_malformed_state_json_returns_none():
    bad = _event(description="Drive\n[drive-planner:meeting=e:dir=outbound]\n<!--dp:{not json}-->")
    assert parse_block(bad) is None


def test_parse_missing_marker_returns_none():
    # state present but no marker -> not a recognizable block
    blob = build_description(
        summary="x",
        meeting_id="e",
        direction="outbound",
        baseline_seconds=10,
        arrive_by=ARRIVE,
        origin="A",
        destination="B",
    )
    no_marker = blob.replace("[drive-planner:meeting=e:dir=outbound]\n", "")
    assert parse_block(_event(description=no_marker)) is None


def test_parse_rejects_newer_schema_version():
    blob = build_description(
        summary="x",
        meeting_id="evt_42",
        direction="outbound",
        baseline_seconds=10,
        arrive_by=ARRIVE,
        origin="A",
        destination="B",
    ).replace(f'"v":{BLOCK_SCHEMA_VERSION}', f'"v":{BLOCK_SCHEMA_VERSION + 1}')
    assert parse_block(_event(description=blob)) is None


def test_parse_rejects_non_int_version():
    # A present-but-non-int version (corrupt or future-shaped record) must read
    # as no-usable-prior-state, not slip through as a missing version.
    blob = build_description(
        summary="x",
        meeting_id="evt_42",
        direction="outbound",
        baseline_seconds=10,
        arrive_by=ARRIVE,
        origin="A",
        destination="B",
    ).replace(f'"v":{BLOCK_SCHEMA_VERSION}', '"v":"x"')
    assert parse_block(_event(description=blob)) is None


# --- leave-by + recheck window -------------------------------------------


def test_baseline_leave_by_subtracts_drive_and_buffer():
    state = parse_block(_event_from_args(_build_args()))
    assert state is not None
    # 1500s drive + 300s default buffer = 30 min before arrive.
    assert state.baseline_leave_by == ARRIVE - timedelta(minutes=30)


def test_due_for_recheck_inside_and_outside_window():
    state = parse_block(_event_from_args(_build_args()))
    assert state is not None
    leave_by = state.baseline_leave_by
    assert state.due_for_recheck(leave_by - timedelta(minutes=40)) is True
    assert state.due_for_recheck(leave_by - timedelta(minutes=50)) is False
    assert state.due_for_recheck(leave_by + timedelta(minutes=10)) is True
    assert state.due_for_recheck(leave_by + timedelta(minutes=20)) is False


# --- alert-suppression record --------------------------------------------


def test_alerted_round_trip():
    assert parse_alerted(serialize_alerted({ALERT_GROWTH, ALERT_LEAVE_NOW})) == frozenset(
        {ALERT_GROWTH, ALERT_LEAVE_NOW}
    )


def test_alerted_strips_whitespace_around_tokens():
    assert parse_alerted(" growth , leave_now ") == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


def test_alerted_non_string_is_empty():
    assert parse_alerted(None) == frozenset()


def test_description_with_alerts_round_trips():
    state = parse_block(_event_from_args(_build_args()))
    assert state is not None
    updated = state.description_with_alerts(frozenset({ALERT_GROWTH}))
    reparsed = parse_block(_event(description=updated))
    assert reparsed is not None
    assert reparsed.already_alerted(ALERT_GROWTH) is True
    assert reparsed.already_alerted(ALERT_LEAVE_NOW) is False
    # everything else preserved
    assert reparsed.baseline_seconds == 1500 and reparsed.origin == state.origin


# --- next_alerts ---------------------------------------------------------


def test_next_alerts_fires_growth_once():
    fire, new = next_alerts(frozenset(), grew=True, leave_now=False)
    assert fire == (ALERT_GROWTH,) and new == frozenset({ALERT_GROWTH})


def test_next_alerts_suppresses_repeat_growth():
    fire, new = next_alerts(frozenset({ALERT_GROWTH}), grew=True, leave_now=False)
    assert fire == () and new == frozenset({ALERT_GROWTH})


def test_next_alerts_fires_leave_now_after_prior_growth():
    fire, new = next_alerts(frozenset({ALERT_GROWTH}), grew=True, leave_now=True)
    assert fire == (ALERT_LEAVE_NOW,) and new == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


def test_next_alerts_silent_when_nothing_new():
    fire, new = next_alerts(frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW}), grew=True, leave_now=True)
    assert fire == () and new == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})
