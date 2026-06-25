"""Tests for the airport drive block planner (`airport_drive.py`).

Deterministic fixtures only — fixed tz-aware datetimes, no generated inputs.
The planner is pure; these exercise the create / recreate / no-op / shift
decisions against a ledger for both directions.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

# E402 suppressed: the sys.path.insert above must execute before this import
# so the skill module resolves by bare name — its bundle dir is only on
# sys.path at runtime, matching nanoclaw-core's import convention.
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


def _dep_desired(**overrides) -> DesiredDriveBlock:
    args = dict(
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


def _plan(desired, ledger=None, events_by_id=None):
    return plan_drive_block(
        flight_id=12345,
        flight_code="DL123",
        desired=desired,
        ledger=ledger or {},
        events_by_id=events_by_id or {},
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


def test_create_when_no_ledger_entry():
    ops = _plan(_dep_desired())
    assert len(ops) == 1
    op = ops[0]
    assert op["op"] == "create"
    assert op["kind"] == KIND_AIRPORT_DRIVE_DEP
    assert op["calendar_id"] == "primary"
    args = op["create_args"]
    assert args["location"] == "BNA"
    assert args["transparency"] == "transparent"  # Free
    assert args["timezone"] == "America/Chicago"
    assert "[flight-assist:flight=12345:dir=to_airport]" in args["description"]
    assert op["signature"] == _dep_desired().signature()


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
    ops = _plan(desired)
    assert ops[0]["kind"] == KIND_AIRPORT_DRIVE_ARR
    assert "timezone" not in ops[0]["create_args"]


# --- plan_drive_block: recreate / no-op / shift ------------------------


def test_recreate_when_tracked_but_live_missing():
    ledger = {KIND_AIRPORT_DRIVE_DEP: {"event_id": "gone", "calendar_id": "primary"}}
    ops = _plan(_dep_desired(), ledger=ledger, events_by_id={})  # live not present
    assert len(ops) == 1
    assert ops[0]["op"] == "create"
    assert "recreate" in ops[0]["reason"]


def test_noop_when_live_and_signature_match():
    sig = _dep_desired().signature()
    ledger = {
        KIND_AIRPORT_DRIVE_DEP: {
            "event_id": "e1",
            "calendar_id": "primary",
            "synced_signature": sig,
        }
    }
    events_by_id = {"e1": {"event_id": "e1", "signature": sig}}
    ops = _plan(_dep_desired(), ledger=ledger, events_by_id=events_by_id)
    assert ops == []


def test_update_when_window_shifted():
    old_sig = "2026-07-02T11:00:00-05:00/2026-07-02T13:00:00-05:00"
    ledger = {
        KIND_AIRPORT_DRIVE_DEP: {
            "event_id": "e1",
            "calendar_id": "primary",
            "synced_signature": old_sig,
        }
    }
    events_by_id = {"e1": {"event_id": "e1", "signature": old_sig}}
    # desired has a different (re-anchored) window than the live event
    ops = _plan(_dep_desired(), ledger=ledger, events_by_id=events_by_id)
    assert len(ops) == 1
    op = ops[0]
    assert op["op"] == "update"
    assert op["event_id"] == "e1"
    assert op["signature"] == _dep_desired().signature()
    assert op["create_args"] is not None  # carries the rebuilt block args
