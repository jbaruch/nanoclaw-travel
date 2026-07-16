"""Tests for the drive-planner block-props codec (`block_props.py`).

Covers the round trip the calendar-as-state design depends on: build the
create-event arguments for a block, then parse a fetched event carrying that
same description back into a `BlockState`. State lives in the event
**description** (the Composio v3 toolkit this plugin shipped on had no writable
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
        "summary": "Drive: Customer sync",
        "leg_start": LEG_START,
        "arrive_by": ARRIVE,
        "baseline_seconds": 1500,
        "origin": "12 Example St, Sampleton, TN 37000",
        "destination": "100 Broadway, Nashville, TN",
    }
    args.update(overrides)
    return build_block_args(**args)


def _event(*, description: str, event_id: str = "block_1", summary: str = "Drive: Customer sync"):
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


# --- create-arg shape (native events.insert body) ------------------------


def test_block_is_free_by_default():
    assert _build_args()["transparency"] == "transparent"


def test_busy_block_is_opaque():
    assert _build_args(busy=True)["transparency"] == "opaque"


def test_create_args_use_nested_start_and_end():
    args = _build_args()
    # nested start/end objects, no flat duration fields
    assert args["start"] == {"dateTime": LEG_START.isoformat()}
    # 12:30 -> 13:00
    assert args["end"] == {"dateTime": ARRIVE.isoformat()}
    assert "start_datetime" not in args
    assert "event_duration_hour" not in args and "event_duration_minutes" not in args
    # destination on the location field; state rides in the description
    assert args["location"] == "100 Broadway, Nashville, TN"
    assert "extendedProperties" not in args


def test_zero_length_leg_is_floored_to_one_minute():
    # Calendar accepts start == end, but the block would render as an
    # invisible hairline. The flat duration contract clamped to >= 1 minute;
    # nested start/end has to do it explicitly.
    args = _build_args(leg_start=LEG_START, arrive_by=LEG_START)
    assert args["start"]["dateTime"] == LEG_START.isoformat()
    assert args["end"]["dateTime"] == (LEG_START + timedelta(minutes=1)).isoformat()


def test_create_args_carry_explicit_timezone():
    # The IANA zone names the event's own timeZone, so a reader sees the block
    # in the venue's clock rather than a bare offset.
    args = _build_args(timezone="America/Chicago")
    assert args["start"]["timeZone"] == "America/Chicago"
    assert args["end"]["timeZone"] == "America/Chicago"


def test_create_args_omit_timezone_when_absent():
    args = _build_args()
    assert "timeZone" not in args["start"]
    assert "timeZone" not in args["end"]


def test_create_wall_clock_expressed_in_target_timezone():
    """#131's live case: a leg computed in the home offset (−05:00) created
    with the venue tz (Europe/London) carries the London wall-clock, so the
    rendered dateTime and the declared timeZone agree.

    Under Composio this was load-bearing for correctness — its adapter dropped
    the offset and re-read the wall-clock in `timezone`, so a Chicago
    wall-clock landed the Rye block 6h early. Natively the offset alone fixes
    the instant; the conversion now only keeps the two fields consistent.
    """
    args = _build_args(
        leg_start=datetime(2026, 7, 10, 9, 15, 46, tzinfo=CT),
        arrive_by=datetime(2026, 7, 10, 9, 45, 0, tzinfo=CT),
        timezone="Europe/London",
    )
    assert args["start"] == {
        "dateTime": "2026-07-10T15:15:46+01:00",
        "timeZone": "Europe/London",
    }


def test_create_wall_clock_converts_etc_gmt_fallback_zone():
    """`scan._extract_timezone`'s offset fallback (`Etc/GMT±N`) is a resolvable
    zone key and converts like a real IANA name (Etc/GMT-1 == UTC+1)."""
    args = _build_args(
        leg_start=datetime(2026, 7, 10, 9, 15, 0, tzinfo=CT),
        arrive_by=datetime(2026, 7, 10, 9, 45, 0, tzinfo=CT),
        timezone="Etc/GMT-1",
    )
    assert args["start"]["dateTime"] == "2026-07-10T15:15:00+01:00"
    assert args["start"]["timeZone"] == "Etc/GMT-1"


def test_unresolvable_timezone_is_dropped_not_sent():
    """A tz string ZoneInfo can't resolve (a raw offset — never emitted by
    `scan._extract_timezone`, but a caller could pass one) must NOT be sent:
    Calendar requires an IANA name and rejects the offset form, which would
    fail the whole create. leg_start's own offset already carries the instant,
    so the block lands correctly with no timeZone at all."""
    args = _build_args(timezone="+01:00")
    assert args["start"] == {"dateTime": LEG_START.isoformat()}
    assert "timeZone" not in args["end"]


def test_description_state_round_trips_via_parse_block():
    state = parse_block(_event_from_args(_build_args()))
    assert isinstance(state, BlockState)
    assert state.meeting_id == "evt_42"
    assert state.direction == "outbound"
    assert state.baseline_seconds == 1500
    assert state.arrive_by == ARRIVE
    assert state.origin.startswith("12 Example St")
    assert state.destination == "100 Broadway, Nashville, TN"
    assert state.summary == "Drive: Customer sync"
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
