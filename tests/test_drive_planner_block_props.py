"""Tests for the drive-planner block-props codec (`block_props.py`).

Covers the round trip the calendar-as-state design depends on: build the
create-event arguments for a block, then parse a fetched event carrying those
same private props back into a `BlockState`. Pins two contracts that, if they
drift, break silently in production:

  * the marker token `build_block_args` writes into the description must match
    `scan._MARKER_RE`, or `scan.py` stops recognizing the planner's own blocks
    (lombot #50 — duplicate blocks);
  * a malformed / non-block event parses to None rather than raising, so one
    bad event can never abort the recheck poll.

Synthetic fixtures only — no live calendar, no real keys.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-planner"))

import block_props  # noqa: E402
from block_props import (  # noqa: E402
    ALERT_GROWTH,
    ALERT_LEAVE_NOW,
    KEY_ALERTED,
    BlockState,
    build_block_args,
    build_marker,
    next_alerts,
    parse_alerted,
    parse_block,
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
        "origin": "1040 Pine Creek Dr, Arrington, TN 37014",
        "destination": "100 Broadway, Nashville, TN",
    }
    args.update(overrides)
    return build_block_args(**args)


def _event_from_args(args: dict, *, event_id: str = "block_1", alerted: str | None = None) -> dict:
    """Shape a fetched event from create-args (private props round-trip)."""
    private = dict(args["extendedProperties"]["private"])
    if alerted is not None:
        private[KEY_ALERTED] = alerted
    return {
        "id": event_id,
        "summary": args["summary"],
        "start": args["start"],
        "end": args["end"],
        "description": args["description"],
        "extendedProperties": {"private": private},
    }


# --- marker contract with scan.py ----------------------------------------


def test_built_marker_matches_scan_regex():
    marker = build_marker("evt_42", "outbound")
    match = _MARKER_RE.search(marker)
    assert match is not None
    assert match["id"] == "evt_42"
    assert match["dir"] == "outbound"


def test_create_args_description_carries_scan_marker():
    args = _build_args()
    match = _MARKER_RE.search(args["description"])
    assert match is not None and match["id"] == "evt_42" and match["dir"] == "outbound"


# --- create-arg shape ----------------------------------------------------


def test_block_is_free_by_default():
    assert _build_args()["transparency"] == "transparent"


def test_busy_block_is_opaque():
    assert _build_args(busy=True)["transparency"] == "opaque"


def test_private_props_carry_machine_state():
    private = _build_args()["extendedProperties"]["private"]
    assert private[block_props.KEY_MEETING] == "evt_42"
    assert private[block_props.KEY_BASELINE] == "1500"
    assert private[block_props.KEY_ARRIVE_BY] == ARRIVE.isoformat()
    assert private[block_props.KEY_DIRECTION] == "outbound"


def test_private_props_carry_schema_version():
    private = _build_args()["extendedProperties"]["private"]
    assert private[block_props.KEY_SCHEMA_VERSION] == str(block_props.BLOCK_SCHEMA_VERSION)


def test_parse_block_rejects_newer_schema_version():
    # A record from a future tile parses to None — no-usable-prior-state, the
    # poll skips it rather than mis-parsing a shape it doesn't understand.
    args = _build_args()
    args["extendedProperties"]["private"][block_props.KEY_SCHEMA_VERSION] = "2"
    assert parse_block(_event_from_args(args)) is None


def test_parse_block_accepts_missing_schema_version():
    # Back-compat: a record without the version key is treated as v1.
    args = _build_args()
    del args["extendedProperties"]["private"][block_props.KEY_SCHEMA_VERSION]
    state = parse_block(_event_from_args(args))
    assert state is not None and state.meeting_id == "evt_42"


def test_leg_end_defaults_to_arrive_by():
    assert _build_args()["end"]["dateTime"] == ARRIVE.isoformat()


# --- input guards --------------------------------------------------------


def test_naive_datetime_rejected():
    import pytest

    with pytest.raises(ValueError, match="timezone-aware"):
        _build_args(arrive_by=datetime(2026, 7, 2, 13, 0))


def test_unknown_direction_rejected():
    import pytest

    with pytest.raises(ValueError, match="outbound/return/bridge"):
        _build_args(direction="sideways")


def test_empty_endpoint_rejected():
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        _build_args(origin="")


# --- parse round trip ----------------------------------------------------


def test_round_trip_parse():
    event = _event_from_args(_build_args())
    state = parse_block(event)
    assert isinstance(state, BlockState)
    assert state.event_id == "block_1"
    assert state.meeting_id == "evt_42"
    assert state.direction == "outbound"
    assert state.baseline_seconds == 1500
    assert state.arrive_by == ARRIVE
    assert state.origin.startswith("1040 Pine Creek")
    assert state.destination == "100 Broadway, Nashville, TN"


def test_parse_non_block_event_returns_none():
    # A plain meeting with no drive-planner private props is not a block.
    assert parse_block({"id": "evt_1", "summary": "Customer sync"}) is None


def test_parse_non_dict_returns_none():
    assert parse_block("garbage") is None
    assert parse_block(None) is None


def test_parse_malformed_baseline_returns_none():
    args = _build_args()
    args["extendedProperties"]["private"][block_props.KEY_BASELINE] = "not-a-number"
    assert parse_block(_event_from_args(args)) is None


def test_parse_missing_arrive_by_returns_none():
    args = _build_args()
    del args["extendedProperties"]["private"][block_props.KEY_ARRIVE_BY]
    assert parse_block(_event_from_args(args)) is None


# --- leave-by + recheck window -------------------------------------------


def test_baseline_leave_by_subtracts_drive_and_buffer():
    state = parse_block(_event_from_args(_build_args()))
    assert state is not None
    # 1500s drive + 300s default buffer = 1800s = 30 min before arrive.
    assert state.baseline_leave_by == ARRIVE - timedelta(minutes=30)


def test_due_for_recheck_inside_and_outside_window():
    state = parse_block(_event_from_args(_build_args()))
    assert state is not None
    leave_by = state.baseline_leave_by
    # 40 min before leave-by: inside the 45-min horizon.
    assert state.due_for_recheck(leave_by - timedelta(minutes=40)) is True
    # 50 min before leave-by: too early.
    assert state.due_for_recheck(leave_by - timedelta(minutes=50)) is False
    # 10 min after leave-by: inside the 15-min departed grace.
    assert state.due_for_recheck(leave_by + timedelta(minutes=10)) is True
    # 20 min after leave-by: past grace, stale.
    assert state.due_for_recheck(leave_by + timedelta(minutes=20)) is False


# --- alert-suppression record --------------------------------------------


def test_alerted_round_trip():
    raw = serialize_alerted({ALERT_GROWTH, ALERT_LEAVE_NOW})
    assert parse_alerted(raw) == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


def test_alerted_drops_unknown_tokens():
    assert parse_alerted("growth,bogus,leave_now") == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


def test_alerted_non_string_is_empty():
    assert parse_alerted(None) == frozenset()


def test_alerted_strips_whitespace_around_tokens():
    assert parse_alerted(" growth , leave_now ") == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


def test_parsed_state_exposes_alerted():
    event = _event_from_args(_build_args(), alerted="growth")
    state = parse_block(event)
    assert state is not None
    assert state.already_alerted(ALERT_GROWTH) is True
    assert state.already_alerted(ALERT_LEAVE_NOW) is False


# --- alert-suppression decision (next_alerts) ----------------------------


def test_next_alerts_fires_growth_once():
    fire, new = next_alerts(frozenset(), grew=True, leave_now=False)
    assert fire == (ALERT_GROWTH,)
    assert new == frozenset({ALERT_GROWTH})


def test_next_alerts_suppresses_repeat_growth():
    fire, new = next_alerts(frozenset({ALERT_GROWTH}), grew=True, leave_now=False)
    assert fire == ()
    assert new == frozenset({ALERT_GROWTH})


def test_next_alerts_fires_leave_now_after_prior_growth():
    fire, new = next_alerts(frozenset({ALERT_GROWTH}), grew=True, leave_now=True)
    assert fire == (ALERT_LEAVE_NOW,)
    assert new == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


def test_next_alerts_fires_both_from_clean_state():
    fire, new = next_alerts(frozenset(), grew=True, leave_now=True)
    assert set(fire) == {ALERT_GROWTH, ALERT_LEAVE_NOW}
    assert new == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


def test_next_alerts_silent_when_nothing_new():
    fire, new = next_alerts(frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW}), grew=True, leave_now=True)
    assert fire == ()
    # unchanged record so the caller skips the suppression patch
    assert new == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})
