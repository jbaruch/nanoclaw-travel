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
        self.patched = []

    def find_events(self, arguments):
        # Ignore the window; tests pass exactly the events they want found.
        # Live v3 FIND_EVENT double-nests: data.event_data.event_data.
        return {"event_data": {"event_data": list(self.events)}}

    def create_event(self, arguments):
        self.created.append(arguments)
        self.events.append({"id": f"new_{len(self.created)}", **arguments})
        return {"id": f"new_{len(self.created)}"}

    def delete_event(self, arguments):
        self.deleted.append(arguments["event_id"])
        self.events = [e for e in self.events if e.get("id") != arguments["event_id"]]
        return {}

    def patch_event(self, arguments):
        self.patched.append(arguments)
        return {"id": arguments["event_id"]}


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
    return {"id": event_id, "summary": args["summary"], "description": args["description"]}


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


def test_remove_with_past_meeting_end_keeps_find_window_valid():
    # A late skip/remove (meeting_end already past) must not invert the find
    # window (timeMax < timeMin), which could fail the whole remove.
    class CapturingComposio(FakeComposio):
        def find_events(self, arguments):
            self.find_args = arguments
            return {"event_data": {"event_data": list(self.events)}}

    args = _create_args()
    client = CapturingComposio(existing=[_fetched_block(args, "block_x")])
    now = datetime(2026, 7, 10, 9, 0, tzinfo=CT)  # well after ARRIVE (2026-07-02)
    past_end = datetime(2026, 7, 2, 13, 0, tzinfo=CT)
    result = apply._remove_mode(
        {"meeting_id": "evt_42", "now": now.isoformat(), "meeting_end": past_end.isoformat()},
        client,
    )
    assert result["skip_recorded"] is True
    assert client.find_args["timeMax"] >= client.find_args["timeMin"]
    # The window must reach back to the past meeting so its blocks are found.
    assert client.find_args["timeMin"] <= past_end.isoformat()


def test_create_tolerates_non_list_meetings():
    client = FakeComposio(existing=[])
    result = apply._create_mode({"meetings": "not-a-list"}, client)  # must not raise
    assert result == {"created": [], "skipped_existing": [], "failed": []}


def test_suppress_tolerates_non_list_patches():
    client = FakeComposio()
    result = apply._suppress_mode({"patches": {"bad": "shape"}}, client)  # must not raise
    assert result == {"patched": []}


def test_create_tolerates_non_dict_start_in_arg():
    # A create-arg whose start/end is present but not a dict must not crash the
    # idempotency-find window math.
    bad = _create_args()
    bad["start"] = None
    client = FakeComposio(existing=[])
    request = {"meetings": [{"meeting_id": "evt_42", "create_args": [bad]}]}
    result = apply._create_mode(request, client)  # must not raise
    assert "evt_42" in {c["meeting_id"] for c in result["created"]} or result["failed"]


def test_remove_treats_delete_404_as_idempotent_success():
    # A concurrent delete (event already gone) surfaces as ComposioError(404);
    # remove must treat it as success, not abort.
    class Gone404(FakeComposio):
        def delete_event(self, arguments):
            raise ComposioError("not found", status_code=404)

    args = _create_args()
    client = Gone404(existing=[_fetched_block(args, "block_x")])
    now = datetime(2026, 7, 2, 9, 0, tzinfo=CT)
    result = apply._remove_mode({"meeting_id": "evt_42", "now": now.isoformat()}, client)
    assert result["skip_recorded"] is True
    assert result["removed"][0]["event_id"] == "block_x"


def test_remove_propagates_non_404_delete_error():
    class Boom(FakeComposio):
        def delete_event(self, arguments):
            raise ComposioError("server error", status_code=500)

    client = Boom(existing=[_fetched_block(_create_args(), "block_x")])
    now = datetime(2026, 7, 2, 9, 0, tzinfo=CT)
    with pytest.raises(ComposioError):
        apply._remove_mode({"meeting_id": "evt_42", "now": now.isoformat()}, client)


def test_create_tolerates_non_list_create_args():
    client = FakeComposio(existing=[])
    request = {"meetings": [{"meeting_id": "evt_42", "create_args": None}]}
    result = apply._create_mode(request, client)  # must not raise
    assert result["created"] == []


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


def test_calendar_id_tolerates_non_dict_first_arg():
    assert apply._calendar_id_of([None, {"calendar_id": "work"}]) == "work"
    assert apply._calendar_id_of(["junk"]) == "primary"


def test_create_skips_malformed_meeting_without_crashing():
    # A meeting entry with no usable id is recorded as failed, not a KeyError.
    client = FakeComposio(existing=[])
    request = {"meetings": [{"create_args": [_create_args()]}, None]}
    result = apply._create_mode(request, client)
    assert client.created == []
    assert len(result["failed"]) == 2
    assert all(f["error"] == "missing meeting_id" for f in result["failed"])


# --- suppress: patch the rebuilt description AFTER the send ---------------


def test_suppress_patches_description():
    client = FakeComposio()
    desc = 'Drive: X\n[drive-planner:meeting=evt_42:dir=outbound]\n<!--dp:{"v":1,"al":"growth"}-->'
    request = {"patches": [{"event_id": "block_x", "calendar_id": "primary", "description": desc}]}
    result = apply._suppress_mode(request, client)
    assert result["patched"] == ["block_x"]
    # The PATCH carries the rebuilt description (state lives there).
    assert client.patched[0]["description"] == desc
    assert client.patched[0]["event_id"] == "block_x"
    assert "extendedProperties" not in client.patched[0]


def test_suppress_treats_patch_404_as_idempotent_skip():
    # A concurrently-deleted block 404s on patch; that must not fail
    # suppression for the other alerts.
    class Patch404(FakeComposio):
        def patch_event(self, arguments):
            if arguments["event_id"] == "gone":
                raise ComposioError("not found", status_code=404)
            self.patched.append(arguments)
            return {}

    client = Patch404()
    request = {
        "patches": [
            {"event_id": "gone", "description": "d1"},
            {"event_id": "live", "description": "d2"},
        ]
    }
    result = apply._suppress_mode(request, client)
    assert result["patched"] == ["live"]


def test_suppress_propagates_non_404_patch_error():
    class Boom(FakeComposio):
        def patch_event(self, arguments):
            raise ComposioError("server error", status_code=500)

    client = Boom()
    request = {"patches": [{"event_id": "x", "description": "d"}]}
    with pytest.raises(ComposioError):
        apply._suppress_mode(request, client)


def test_suppress_skips_malformed_patch():
    client = FakeComposio()
    request = {
        "patches": [{"event_id": "", "description": "d"}, {"event_id": "ok", "description": 5}]
    }
    result = apply._suppress_mode(request, client)
    assert result["patched"] == []
    assert client.patched == []


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
