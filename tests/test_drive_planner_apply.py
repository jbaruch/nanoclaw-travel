"""Tests for the drive-planner calendar-write step (`apply.py`).

Uses a fake in-memory Composio client (no live backend) to exercise the
idempotent create (lombot #50 — never double-book a meeting that already has a
marker block) and the skip-remove (delete the meeting's blocks + record a skip
so the next sweep won't recreate them). Skip state is redirected to a tmp dir
via DRIVE_PLANNER_STATE_DIR so the test owns its own state.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVE = REPO_ROOT / "skills" / "drive-planner"
sys.path.insert(0, str(DRIVE))

import apply  # noqa: E402
import skip_state  # noqa: E402
from block_props import build_block_args  # noqa: E402

# apply.py adds the flight-assist bundle to sys.path on import (for ComposioError);
# that makes composio_client importable here too.
from composio_client import ComposioError  # noqa: E402

CT = timezone(timedelta(hours=-5))
ARRIVE = datetime(2026, 7, 2, 13, 0, tzinfo=CT)
LEG_START = datetime(2026, 7, 2, 12, 30, tzinfo=CT)


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))


class FakeComposio:
    """In-memory stand-in for ComposioClient: stores created events, serves finds."""

    def __init__(self, existing=None):
        self.events = list(existing or [])
        self.created = []
        self.deleted = []

    def find_events(self, arguments):
        # Ignore the window; tests pass exactly the events they want found.
        return {"items": list(self.events)}

    def create_event(self, arguments):
        self.created.append(arguments)
        self.events.append({"id": f"new_{len(self.created)}", **arguments})
        return {"id": f"new_{len(self.created)}"}

    def delete_event(self, arguments):
        self.deleted.append(arguments["event_id"])
        self.events = [e for e in self.events if e.get("id") != arguments["event_id"]]
        return {}


def _create_args(meeting_id="evt_42", direction="outbound"):
    return build_block_args(
        calendar_id="primary",
        meeting_id=meeting_id,
        direction=direction,
        summary="Drive to Customer sync",
        leg_start=LEG_START,
        arrive_by=ARRIVE,
        baseline_seconds=1500,
        origin="Home",
        destination="100 Broadway, Nashville, TN",
    )


def _fetched_block(args, event_id="block_1"):
    return {
        "id": event_id,
        "description": args["description"],
        "extendedProperties": args["extendedProperties"],
    }


# --- create: idempotency (lombot #50) ------------------------------------


def test_create_inserts_when_no_existing_block():
    client = FakeComposio(existing=[])
    request = {"meetings": [{"meeting_id": "evt_42", "create_args": [_create_args()]}]}
    result = apply._create_mode(request, client)
    assert len(result["created"]) == 1
    assert result["created"][0]["direction"] == "outbound"
    assert result["skipped_existing"] == []
    assert len(client.created) == 1


def test_create_is_idempotent_when_block_exists():
    args = _create_args()
    client = FakeComposio(existing=[_fetched_block(args)])
    request = {"meetings": [{"meeting_id": "evt_42", "create_args": [args]}]}
    result = apply._create_mode(request, client)
    # The marker block already exists — create is a no-op (no duplicate).
    assert result["created"] == []
    assert result["skipped_existing"] == [{"meeting_id": "evt_42", "direction": "outbound"}]
    assert client.created == []


def test_create_adds_only_missing_direction():
    outbound = _create_args(direction="outbound")
    ret = _create_args(direction="return")
    # Outbound already on the calendar; only the return should be created.
    client = FakeComposio(existing=[_fetched_block(outbound, "ob")])
    request = {"meetings": [{"meeting_id": "evt_42", "create_args": [outbound, ret]}]}
    result = apply._create_mode(request, client)
    assert [c["direction"] for c in result["created"]] == ["return"]
    assert result["skipped_existing"] == [{"meeting_id": "evt_42", "direction": "outbound"}]


def test_create_records_per_leg_failure_without_aborting():
    class FlakyComposio(FakeComposio):
        def create_event(self, arguments):
            raise ComposioError("composio 500")

    client = FlakyComposio(existing=[])
    request = {"meetings": [{"meeting_id": "evt_42", "create_args": [_create_args()]}]}
    result = apply._create_mode(request, client)
    assert result["created"] == []
    assert len(result["failed"]) == 1
    assert "composio 500" in result["failed"][0]["error"]


# --- remove: delete blocks + record skip ---------------------------------


def test_remove_deletes_blocks_and_records_skip():
    args = _create_args()
    client = FakeComposio(existing=[_fetched_block(args, "block_x")])
    now = datetime(2026, 7, 2, 9, 0, tzinfo=CT)
    request = {
        "meeting_id": "evt_42",
        "now": now.isoformat(),
        "meeting_end": ARRIVE.isoformat(),
        "calendar_id": "primary",
    }
    result = apply._remove_mode(request, client)
    assert result["skip_recorded"] is True
    assert client.deleted == ["block_x"]
    assert result["removed"][0]["event_id"] == "block_x"
    # The skip is now active, so the next sweep would bucket evt_42 as skipped.
    assert "evt_42" in skip_state.load_active_skips(now)


def test_remove_only_touches_the_named_meeting():
    keep = _fetched_block(_create_args(meeting_id="evt_OTHER"), "keep")
    drop = _fetched_block(_create_args(meeting_id="evt_42"), "drop")
    client = FakeComposio(existing=[keep, drop])
    now = datetime(2026, 7, 2, 9, 0, tzinfo=CT)
    request = {"meeting_id": "evt_42", "now": now.isoformat(), "meeting_end": ARRIVE.isoformat()}
    apply._remove_mode(request, client)
    assert client.deleted == ["drop"]


def test_remove_requires_meeting_id():
    with pytest.raises(ValueError, match="meeting_id"):
        apply._remove_mode({"now": "x", "meeting_end": "y"}, FakeComposio())


def test_remove_derives_skip_expiry_from_block_when_no_meeting_end():
    # The skip-reply path carries only the meeting id + now; expiry is derived
    # from the deleted block's arrive-by (the meeting start).
    args = _create_args()
    client = FakeComposio(existing=[_fetched_block(args, "block_x")])
    now = datetime(2026, 7, 2, 9, 0, tzinfo=CT)
    result = apply._remove_mode({"meeting_id": "evt_42", "now": now.isoformat()}, client)
    assert result["skip_recorded"] is True
    assert client.deleted == ["block_x"]
    assert "evt_42" in skip_state.load_active_skips(now)
    # Skip stays active right up to the meeting start, then expires.
    assert "evt_42" not in skip_state.load_active_skips(ARRIVE + timedelta(minutes=1))
