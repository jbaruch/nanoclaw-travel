"""Tests for the airport drive block planner (`airport_drive.py`).

Deterministic fixtures only — fixed tz-aware datetimes, no generated inputs.
The planner is pure; these exercise the create / no-op / shift decisions by
scanning fetched calendar events for the block's marker (no ledger).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

# E402 suppressed: the sys.path.insert above must execute before this import
# so the skill module resolves by bare name — its bundle dir is only on
# sys.path at runtime, matching nanoclaw-core's import convention.
from airport_block import build_description  # noqa: E402
from airport_drive import (  # noqa: E402
    KIND_AIRPORT_DRIVE_ARR,
    KIND_AIRPORT_DRIVE_DEP,
    AirportDrivePlanError,
    DesiredDriveBlock,
    plan_drive_block,
)

CT = timezone(timedelta(hours=-5))
DEP = datetime(2026, 7, 2, 14, 0, tzinfo=CT)
BE_AT_AIRPORT = DEP - timedelta(minutes=60)  # dep − domestic clearance
LEAVE_BY = BE_AT_AIRPORT - timedelta(minutes=30)  # − 30-min drive


def _dep_desired(**overrides: Any) -> DesiredDriveBlock:
    args: dict[str, Any] = dict(
        direction="to_airport",
        summary="Drive: → BNA (DL123)",
        leg_start=LEAVE_BY,
        anchor=BE_AT_AIRPORT,
        baseline_seconds=1800,
        origin="<live location>",
        destination="BNA",
        timezone="America/Chicago",
    )
    args.update(overrides)
    return DesiredDriveBlock(**args)


def _existing_event(desired, *, flight_id="12345", event_id="evt_abc", signature=None) -> dict:
    """A fetched calendar event whose description carries `desired`'s marker."""
    desc = build_description(
        summary=desired.summary,
        flight_id=flight_id,
        direction=desired.direction,
        baseline_seconds=desired.baseline_seconds,
        anchor=desired.anchor,
        origin=desired.origin,
        destination=desired.destination,
    )
    return {
        "id": event_id,
        "description": desc,
        "calendar_id": "primary",
        "signature": signature if signature is not None else desired.signature(),
    }


def _plan(desired, events=None):
    return plan_drive_block(
        flight_id=12345,
        flight_code="DL123",
        desired=desired,
        events=events or [],
        calendar_id="primary",
    )


# --- DesiredDriveBlock --------------------------------------------------


def test_kind_maps_by_direction():
    assert _dep_desired().kind == KIND_AIRPORT_DRIVE_DEP
    assert _dep_desired(direction="from_airport").kind == KIND_AIRPORT_DRIVE_ARR


def test_unknown_direction_raises():
    with pytest.raises(AirportDrivePlanError):
        _ = _dep_desired(direction="sideways").kind


def test_signature_uses_start_and_end():
    d = _dep_desired()
    assert d.signature() == f"{LEAVE_BY.isoformat()}/{BE_AT_AIRPORT.isoformat()}"


def test_signature_from_airport_uses_explicit_leg_end():
    anchor = datetime(2026, 7, 2, 18, 40, tzinfo=CT)
    home_eta = anchor + timedelta(minutes=45)
    d = _dep_desired(
        direction="from_airport",
        leg_start=anchor,
        anchor=anchor,
        leg_end=home_eta,
        baseline_seconds=2700,
    )
    assert d.signature() == f"{anchor.isoformat()}/{home_eta.isoformat()}"


# --- plan_drive_block: create ------------------------------------------


def test_create_when_no_block_on_calendar():
    ops = _plan(_dep_desired(), events=[])
    assert len(ops) == 1
    op = ops[0]
    assert op["op"] == "create"
    assert op["kind"] == KIND_AIRPORT_DRIVE_DEP
    assert op["calendar_id"] == "primary"
    args = op["create_args"]
    assert args["location"] == "BNA"
    assert args["transparency"] == "transparent"  # Free
    assert args["start"]["timeZone"] == "America/Chicago"
    assert args["calendar_id"] == op["calendar_id"]  # never diverge
    assert "[flight-assist:flight=12345:dir=to_airport]" in args["description"]
    assert op["signature"] == _dep_desired().signature()


def test_create_ignores_other_flights_and_non_blocks():
    other_flight = _existing_event(_dep_desired(), flight_id="99999", event_id="other")
    other_dir = _existing_event(_dep_desired(direction="from_airport"), event_id="arr_evt")
    junk = {"id": "x", "description": "just a meeting, no marker", "signature": "a/b"}
    ops = _plan(_dep_desired(), events=[other_flight, other_dir, junk])
    assert ops[0]["op"] == "create"  # none match this flight+to_airport


def test_create_from_airport_kind():
    anchor = datetime(2026, 7, 2, 18, 40, tzinfo=CT)
    desired = _dep_desired(
        direction="from_airport",
        summary="Drive: BNA → home",
        leg_start=anchor,
        anchor=anchor,
        leg_end=anchor + timedelta(minutes=45),
        baseline_seconds=2700,
        origin="BNA",
        destination="home",
        timezone=None,
    )
    ops = _plan(desired, events=[])
    assert ops[0]["kind"] == KIND_AIRPORT_DRIVE_ARR
    assert "timeZone" not in ops[0]["create_args"]["start"]


# --- plan_drive_block: no-op / shift -----------------------------------


def test_noop_when_existing_window_matches():
    desired = _dep_desired()
    events = [_existing_event(desired)]  # signature == desired.signature()
    assert _plan(desired, events=events) == []


def test_update_when_window_shifted():
    desired = _dep_desired()
    old_sig = "2026-07-02T11:00:00-05:00/2026-07-02T13:00:00-05:00"
    events = [_existing_event(desired, event_id="e1", signature=old_sig)]
    ops = _plan(desired, events=events)
    assert len(ops) == 1
    op = ops[0]
    assert op["op"] == "update"
    assert op["event_id"] == "e1"
    assert op["signature"] == desired.signature()
    assert op["create_args"] is not None  # carries the rebuilt block args
    assert op["create_args"]["calendar_id"] == op["calendar_id"]
