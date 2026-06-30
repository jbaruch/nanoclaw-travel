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
        summary="Drive: Customer sync",
        leg_start=LEG_START,
        arrive_by=ARRIVE,
        baseline_seconds=1500,
        origin="Home",
        destination="100 Broadway, Nashville, TN",
    )


def _fetched_block(args, event_id="block_1"):
    return {"id": event_id, "summary": args["summary"], "description": args["description"]}


def _block_for(meeting_id, summary, arrive, event_id):
    args = build_block_args(
        calendar_id="primary",
        meeting_id=meeting_id,
        direction="outbound",
        summary=f"Drive: {summary}",
        leg_start=arrive - timedelta(minutes=30),
        arrive_by=arrive,
        baseline_seconds=1500,
        origin="Home",
        destination="venue",
    )
    return _fetched_block(args, event_id)


# --- list: cancel-UX block listing (#86) ---------------------------------


def test_list_returns_one_per_meeting_ordered_by_leave_by_no_drive_prefix():
    early = _block_for(
        "evt_early", "Swimming Practice", datetime(2026, 7, 2, 9, 30, tzinfo=CT), "b1"
    )
    late = _block_for(
        "evt_late", "Football Practice", datetime(2026, 7, 2, 15, 30, tzinfo=CT), "b2"
    )
    client = FakeComposio(existing=[late, early])  # unordered on the calendar
    result = apply._list_mode({"now": datetime(2026, 7, 2, 8, 0, tzinfo=CT).isoformat()}, client)
    # ordered by leave_by; the "Drive: " prefix is stripped for the operator
    assert [b["summary"] for b in result["blocks"]] == ["Swimming Practice", "Football Practice"]
    assert result["blocks"][0]["meeting_id"] == "evt_early"


def test_list_groups_multiple_legs_of_one_meeting():
    out = _block_for("evt_42", "Customer sync", datetime(2026, 7, 2, 13, 0, tzinfo=CT), "ob")
    ret = _block_for("evt_42", "Customer sync", datetime(2026, 7, 2, 15, 0, tzinfo=CT), "rt")
    client = FakeComposio(existing=[out, ret])
    result = apply._list_mode({"now": datetime(2026, 7, 2, 8, 0, tzinfo=CT).isoformat()}, client)
    assert len(result["blocks"]) == 1  # one entry per meeting, not per leg
    assert result["blocks"][0]["meeting_id"] == "evt_42"


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


def test_remove_by_summary_picks_the_right_meeting_among_pre_existing_blocks():
    # The cancel UX resolves by name, never list position — a pre-existing block
    # for another meeting must not be hit by "cancel <name>" (#86 / the OpenAI
    # ordinal-shift concern). Two blocks on the calendar; remove "Football
    # Practice" by summary deletes only its block.
    swim = _block_for(
        "evt_swim", "Swimming Practice", datetime(2026, 7, 2, 9, 30, tzinfo=CT), "b_sw"
    )
    football = _block_for(
        "evt_fb", "Football Practice", datetime(2026, 7, 2, 15, 30, tzinfo=CT), "b_fb"
    )
    client = FakeComposio(existing=[swim, football])
    now = datetime(2026, 7, 2, 8, 0, tzinfo=CT)
    result = apply._remove_mode({"summary": "Football Practice", "now": now.isoformat()}, client)
    assert client.deleted == ["b_fb"]  # only football, not swimming
    assert result["skip_recorded"] is True
    assert "evt_fb" in skip_state.load_active_skips(now)
    assert "evt_swim" not in skip_state.load_active_skips(now)


def test_remove_by_summary_skips_unplannable_meeting_with_no_block():
    # An unplannable meeting has no drive block, so resolution falls back to the
    # meeting event itself — the skip still records, giving it a working cancel
    # path without exposing an id (#86 / the OpenAI unplannable-skip concern).
    # The skip expiry anchors to the meeting's END, not the 30-day fallback,
    # matching the documented contract (stateful-artifacts / state-schema.md).
    meeting_end = datetime(2026, 7, 2, 15, 0, tzinfo=CT)
    meeting_event = {
        "id": "evt_flight",
        "summary": "St. Louis talk",
        "description": "",
        "start": {"dateTime": datetime(2026, 7, 2, 14, 0, tzinfo=CT).isoformat()},
        "end": {"dateTime": meeting_end.isoformat()},
    }
    client = FakeComposio(existing=[meeting_event])
    now = datetime(2026, 7, 2, 8, 0, tzinfo=CT)
    result = apply._remove_mode({"summary": "St. Louis talk", "now": now.isoformat()}, client)
    assert result["removed"] == []  # no block to delete
    assert result["skip_recorded"] is True
    skips = skip_state.load_active_skips(now)
    assert "evt_flight" in skips
    # expiry is the meeting end, not now + 30-day fallback
    assert skips["evt_flight"] == meeting_end.isoformat()


def test_resolve_candidates_reports_return_block_actual_start():
    # A return block's stored arrive_by is its leg END; its real start is the
    # meeting end, not the buffer-shifted baseline_leave_by (Copilot return-leg
    # concern). The candidate must carry the real start so disambiguation lines
    # up with the calendar.
    meeting_end = datetime(2026, 7, 2, 15, 0, tzinfo=CT)
    ret_args = build_block_args(
        calendar_id="primary",
        meeting_id="evt_ret",
        direction="return",
        summary="Drive: Solo meeting",
        leg_start=meeting_end,
        arrive_by=meeting_end + timedelta(seconds=1500),
        baseline_seconds=1500,
        origin="venue",
        destination="Home",
    )
    ret = _fetched_block(ret_args, "b_ret")
    assert apply._resolve_candidates([ret], "Solo meeting") == [
        ("evt_ret", meeting_end.isoformat())
    ]


def test_remove_by_unmatched_summary_reports_no_match():
    client = FakeComposio(existing=[])
    now = datetime(2026, 7, 2, 8, 0, tzinfo=CT)
    result = apply._remove_mode({"summary": "Nonexistent", "now": now.isoformat()}, client)
    assert result["skip_recorded"] is False
    assert result["unmatched_summary"] == "Nonexistent"


def test_remove_same_summary_without_leave_by_is_ambiguous_not_guessed():
    # Two "Standup" meetings (recurring) — summary alone is ambiguous, so the
    # script returns the choices instead of guessing by fetch order.
    mon = _block_for("evt_mon", "Standup", datetime(2026, 7, 6, 9, 0, tzinfo=CT), "b_mon")
    tue = _block_for("evt_tue", "Standup", datetime(2026, 7, 7, 9, 0, tzinfo=CT), "b_tue")
    client = FakeComposio(existing=[tue, mon])
    now = datetime(2026, 7, 6, 7, 0, tzinfo=CT)
    result = apply._remove_mode({"summary": "Standup", "now": now.isoformat()}, client)
    assert result["skip_recorded"] is False
    assert result["ambiguous_summary"] == "Standup"
    assert len(result["candidates"]) == 2
    assert client.deleted == []  # nothing guessed/deleted


def test_remove_resolves_unplannable_occurrence_even_when_another_has_a_block():
    # Monday "Standup" has a drive block; Tuesday "Standup" is unplannable (only
    # the meeting event exists). Cancelling Tuesday must still resolve + skip it,
    # not be masked by Monday's block (the OpenAI collision case).
    mon_block = _block_for("evt_mon", "Standup", datetime(2026, 7, 6, 9, 0, tzinfo=CT), "b_mon")
    tue_meeting = {
        "id": "evt_tue",
        "summary": "Standup",
        "description": "",
        "start": {"dateTime": datetime(2026, 7, 7, 9, 0, tzinfo=CT).isoformat()},
    }
    client = FakeComposio(existing=[mon_block, tue_meeting])
    now = datetime(2026, 7, 6, 7, 0, tzinfo=CT)
    # both occurrences are offered (block + unplannable event), not just the block
    amb = apply._remove_mode({"summary": "Standup", "now": now.isoformat()}, client)
    assert {c["leave_by"] for c in amb["candidates"]} == {
        datetime(2026, 7, 6, 8, 30, tzinfo=CT).isoformat(),  # Monday block leave-by
        datetime(2026, 7, 7, 9, 0, tzinfo=CT).isoformat(),  # Tuesday meeting start
    }
    # pin Tuesday (the unplannable one) — it skips even though it has no block
    res = apply._remove_mode(
        {
            "summary": "Standup",
            "leave_by": datetime(2026, 7, 7, 9, 0, tzinfo=CT).isoformat(),
            "now": now.isoformat(),
        },
        client,
    )
    assert res["removed"] == [] and res["skip_recorded"] is True
    assert "evt_tue" in skip_state.load_active_skips(now)


def test_resolve_candidates_uses_earliest_leg_leave_by():
    # A meeting's outbound + return legs have different leave-bys; the candidate
    # must report the EARLIEST (outbound), matching what `list` shows, so the
    # leave_by disambiguation lines up regardless of calendar fetch order.
    out = _block_for("evt_42", "Customer sync", datetime(2026, 7, 2, 13, 0, tzinfo=CT), "ob")
    ret = _block_for("evt_42", "Customer sync", datetime(2026, 7, 2, 16, 0, tzinfo=CT), "rt")
    candidates = apply._resolve_candidates([ret, out], "Customer sync")  # return seen first
    assert candidates == [("evt_42", datetime(2026, 7, 2, 12, 30, tzinfo=CT).isoformat())]


def test_remove_same_summary_with_leave_by_pins_the_right_instance():
    mon = _block_for("evt_mon", "Standup", datetime(2026, 7, 6, 9, 0, tzinfo=CT), "b_mon")
    tue = _block_for("evt_tue", "Standup", datetime(2026, 7, 7, 9, 0, tzinfo=CT), "b_tue")
    client = FakeComposio(existing=[tue, mon])
    now = datetime(2026, 7, 6, 7, 0, tzinfo=CT)
    # Tuesday's leave-by = 9:00 - 25min drive - 5min buffer = 8:30 on 2026-07-07.
    tue_leave_by = datetime(2026, 7, 7, 8, 30, tzinfo=CT).isoformat()
    result = apply._remove_mode(
        {"summary": "Standup", "leave_by": tue_leave_by, "now": now.isoformat()}, client
    )
    assert result["skip_recorded"] is True
    assert client.deleted == ["b_tue"]  # only Tuesday's block
    assert "evt_tue" in skip_state.load_active_skips(now)
    assert "evt_mon" not in skip_state.load_active_skips(now)


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
    assert result == {"created": [], "skipped_existing": [], "failed": [], "message": None}


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


# --- build_notification: id-free, skip-by-number sweep message -------------


def _meeting(meeting_id, summary, *, leave_by, drive_minutes, route_errors=None, unplannable=None):
    return {
        "meeting_id": meeting_id,
        "summary": summary,
        "leave_by": leave_by,
        "drive_minutes": drive_minutes,
        "route_errors": route_errors or [],
        "unplannable": unplannable or [],
    }


def test_notification_single_block_says_reply_skip():
    meetings = [
        _meeting(
            "evt_1", "Football practice", leave_by="2026-05-13T15:28:00-07:00", drive_minutes=27
        )
    ]
    created = [{"meeting_id": "evt_1", "direction": "outbound"}]
    msg = apply.build_notification(meetings, created, [], [])
    assert msg == (
        "Added a drive block for Football practice — leave by 3:28 PM "
        "(27-min drive with current traffic).\n"
        "Reply skip if you're not driving."
    )


def test_notification_single_block_never_includes_meeting_id():
    meetings = [
        _meeting(
            "ccc2067fqsb2qvf6hh6n1uvfk6",
            "Football practice",
            leave_by="2026-05-13T15:28:00-07:00",
            drive_minutes=27,
        )
    ]
    created = [{"meeting_id": "ccc2067fqsb2qvf6hh6n1uvfk6", "direction": "outbound"}]
    msg = apply.build_notification(meetings, created, [], [])
    assert "ccc2067fqsb2qvf6hh6n1uvfk6" not in msg


def test_notification_multiple_blocks_numbered_skip_by_number():
    meetings = [
        _meeting("evt_2", "Dentist", leave_by="2026-05-13T09:10:00-05:00", drive_minutes=15),
        _meeting(
            "evt_1", "Football practice", leave_by="2026-05-13T15:28:00-05:00", drive_minutes=27
        ),
    ]
    created = [
        {"meeting_id": "evt_1", "direction": "outbound"},
        {"meeting_id": "evt_2", "direction": "outbound"},
    ]
    msg = apply.build_notification(meetings, created, [], [])
    lines = msg.split("\n")
    # Ordered by leave_by: Dentist (9:10) before Football (3:28 PM).
    assert lines[0] == "Added drive blocks:"
    assert lines[1] == "1. Dentist — leave by 9:10 AM (15-min drive)"
    assert lines[2] == "2. Football practice — leave by 3:28 PM (27-min drive)"
    assert lines[3] == "Reply skip 1, or skip 1 and 3, to drop any."


def test_notification_return_only_block_has_no_leave_by():
    meetings = [_meeting("evt_3", "Swim", leave_by=None, drive_minutes=None)]
    created = [{"meeting_id": "evt_3", "direction": "return"}]
    msg = apply.build_notification(meetings, created, [], [])
    assert msg == ("Added a return drive block for Swim.\nReply skip if you're not driving.")


def test_notification_silent_when_nothing_changed():
    meetings = [
        _meeting("evt_4", "Already handled", leave_by="2026-05-13T10:00:00-05:00", drive_minutes=10)
    ]
    # No created legs, only an idempotent skip — nothing to announce.
    skipped = [{"meeting_id": "evt_4", "direction": "outbound"}]
    assert apply.build_notification(meetings, [], skipped, []) is None


def test_notification_fully_unplannable_offers_mute():
    meetings = [
        _meeting(
            "evt_5",
            "Offsite",
            leave_by=None,
            drive_minutes=None,
            unplannable=[{"direction": "outbound", "reason": "the operator likely flew"}],
        )
    ]
    msg = apply.build_notification(meetings, [], [], [])
    assert "No outbound drive block for Offsite — the operator likely flew." in msg
    assert "Reply don't drive to Offsite to stop seeing it." in msg


def test_notification_surfaces_route_errors_and_failures():
    meetings = [
        _meeting(
            "evt_6",
            "Clinic",
            leave_by="2026-05-13T08:00:00-05:00",
            drive_minutes=12,
            route_errors=[{"direction": "outbound", "error": "ZERO_RESULTS"}],
        ),
    ]
    msg = apply.build_notification(meetings, [], [], [])
    assert "Couldn't compute drive time for Clinic (ZERO_RESULTS)" in msg


def test_create_mode_emits_ready_to_send_message():
    client = FakeComposio(existing=[])
    request = {
        "meetings": [
            {
                "meeting_id": "evt_42",
                "summary": "Customer sync",
                "leave_by": "2026-05-02T12:30:00-05:00",
                "drive_minutes": 25,
                "create_args": [_create_args()],
                "route_errors": [],
                "unplannable": [],
            }
        ]
    }
    result = apply._create_mode(request, client)
    assert result["created"] == [{"meeting_id": "evt_42", "direction": "outbound"}]
    assert result["message"] == (
        "Added a drive block for Customer sync — leave by 12:30 PM "
        "(25-min drive with current traffic).\n"
        "Reply skip if you're not driving."
    )
