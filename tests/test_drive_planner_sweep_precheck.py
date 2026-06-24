"""Tests for the drive-planner sweep precheck planning core (`precheck.py`).

Exercises `plan_meetings` with an injected router (no live Composio, no live
maps) over scan output, plus the round trip back through `parse_block` so the
blocks the sweep builds are exactly what the recheck poll later reads. Covers:
the actionable gate (only needs_decision / bridge / back_to_back surface),
per-leg create-args, return-leg handling, and route-error recording (a leg the
router can't price is reported, never silently dropped — Epic #59 §5).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVE = REPO_ROOT / "skills" / "drive-planner"
sys.path.insert(0, str(DRIVE))

from block_props import parse_block  # noqa: E402
from route_error import RouteError  # noqa: E402
from scan import scan  # noqa: E402


def _load(name: str, path: Path):
    # drive-planner's precheck.py shares the bare module name `precheck` with
    # flight-assist's; load it under a unique name so the two never shadow each
    # other in sys.modules (the convention conftest._load / the other precheck
    # tests use).
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


precheck = _load("drive_planner_sweep_precheck", DRIVE / "precheck.py")

CT = timezone(timedelta(hours=-5))
NOW = datetime(2026, 7, 1, 8, 0, tzinfo=CT)
HOME = "1040 Pine Creek Dr, Arrington, TN 37014"


def _meeting(eid: str, start_h: int, *, location: str, end_h: int | None = None) -> dict:
    end_h = end_h if end_h is not None else start_h + 1
    return {
        "id": eid,
        "summary": f"Meeting {eid}",
        "location": location,
        "start": {"dateTime": datetime(2026, 7, 1, start_h, 0, tzinfo=CT).isoformat()},
        "end": {"dateTime": datetime(2026, 7, 1, end_h, 0, tzinfo=CT).isoformat()},
        "description": "",
    }


def _fixed_router(seconds: int):
    return lambda origin, destination: seconds


def _scan(events):
    return scan(events, now=NOW, home_address=HOME)


def _dir(create_arg: dict) -> str:
    return create_arg["extendedProperties"]["private"]["drive_planner_dir"]


def _leg(create_args: list[dict], direction: str) -> dict:
    return next(a for a in create_args if _dir(a) == direction)


# --- the actionable gate -------------------------------------------------


def test_no_actionable_meetings_yields_empty():
    # A virtual meeting filters out; nothing actionable.
    events = [_meeting("v", 14, location="https://zoom.us/j/123")]
    payload = precheck.plan_meetings(_scan(events), route=_fixed_router(900), home_address=HOME)
    assert payload["meetings"] == []


def test_back_to_back_meeting_with_no_legs_does_not_wake():
    # Three same-venue zero-duration meetings one hour apart (a tight gap, well
    # under the 90-min threshold): the MIDDLE one is back_to_back with no
    # transit legs (stay put both sides). It must not surface — waking the agent
    # with nothing to do violates the "wake only when actionable" contract.
    venue = "100 Broadway, Nashville, TN"
    events = [
        _meeting("a", 14, end_h=14, location=venue),
        _meeting("b", 15, end_h=15, location=venue),
        _meeting("c", 16, end_h=16, location=venue),
    ]
    payload = precheck.plan_meetings(_scan(events), route=_fixed_router(1500), home_address=HOME)
    surfaced = {m["meeting_id"] for m in payload["meetings"]}
    assert "b" not in surfaced  # middle, no legs → skipped


def test_standalone_meeting_is_actionable_with_two_legs():
    events = [_meeting("m1", 14, location="100 Broadway, Nashville, TN")]
    payload = precheck.plan_meetings(_scan(events), route=_fixed_router(1500), home_address=HOME)
    [m] = payload["meetings"]
    assert m["meeting_id"] == "m1"
    assert m["bucket"] == "needs_decision"
    directions = sorted(_dir(a) for a in m["create_args"])
    assert directions == ["outbound", "return"]
    assert m["route_errors"] == []


# --- leg geometry --------------------------------------------------------


def test_outbound_block_starts_baseline_plus_buffer_before_meeting():
    events = [_meeting("m1", 14, location="100 Broadway, Nashville, TN")]
    payload = precheck.plan_meetings(_scan(events), route=_fixed_router(1500), home_address=HOME)
    [m] = payload["meetings"]
    outbound = _leg(m["create_args"], "outbound")
    # arrive_by = 14:00; baseline 1500s (25m) + 300s buffer (5m) = 30m before.
    assert outbound["start"]["dateTime"] == datetime(2026, 7, 1, 13, 30, tzinfo=CT).isoformat()
    assert outbound["end"]["dateTime"] == datetime(2026, 7, 1, 14, 0, tzinfo=CT).isoformat()
    assert outbound["extendedProperties"]["private"]["drive_planner_baseline_seconds"] == "1500"


def test_return_block_starts_at_meeting_end():
    events = [_meeting("m1", 14, end_h=15, location="100 Broadway, Nashville, TN")]
    payload = precheck.plan_meetings(_scan(events), route=_fixed_router(1500), home_address=HOME)
    [m] = payload["meetings"]
    ret = _leg(m["create_args"], "return")
    assert ret["start"]["dateTime"] == datetime(2026, 7, 1, 15, 0, tzinfo=CT).isoformat()
    # return leg ends baseline seconds after departure (15:00 + 25m)
    assert ret["start"]["dateTime"] < ret["end"]["dateTime"]


# --- round trip: built blocks parse back for the recheck poll ------------


def test_built_outbound_block_round_trips_to_blockstate():
    events = [_meeting("m1", 14, location="100 Broadway, Nashville, TN")]
    payload = precheck.plan_meetings(_scan(events), route=_fixed_router(1500), home_address=HOME)
    [m] = payload["meetings"]
    outbound = _leg(m["create_args"], "outbound")
    # Shape a fetched event from the create-args and parse it back.
    fetched = {
        "id": "block_evt",
        "description": outbound["description"],
        "extendedProperties": outbound["extendedProperties"],
    }
    state = parse_block(fetched)
    assert state is not None
    assert state.meeting_id == "m1"
    assert state.baseline_seconds == 1500
    assert state.direction == "outbound"


# --- no silent miss: unpriced legs are reported --------------------------


def test_route_failure_is_recorded_not_dropped():
    def boom(origin, destination):
        raise RouteError("ALL_PROVIDERS_FAILED")

    events = [_meeting("m1", 14, location="100 Broadway, Nashville, TN")]
    payload = precheck.plan_meetings(_scan(events), route=boom, home_address=HOME)
    [m] = payload["meetings"]
    assert m["create_args"] == []
    assert len(m["route_errors"]) == 2  # outbound + return both failed
    assert all("ALL_PROVIDERS_FAILED" in e["error"] for e in m["route_errors"])
