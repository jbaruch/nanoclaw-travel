"""Tests for the calendar reconciliation orchestrator (`calendar_reconcile.py`).

Deterministic fixtures only: a fake calendar client records calls and returns
controlled responses, and state lives under a tmp `FLIGHT_ASSIST_STATE_DIR`.
No network, no real calendar IDs, no real API keys. Assertions target
outcomes — the ledger written back, which calendar calls fired, the summary —
not the planner's internals (those are covered by `test_calendar_plan.py`).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import must

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

import calendar_reconcile as cr  # noqa: E402
from calendar_plan import _signature as _sig  # noqa: E402
from calendar_tags import encode_tags  # noqa: E402
from google_calendar_client import GoogleCalendarError  # noqa: E402
from state import (  # noqa: E402
    read_config,
    read_flight_state,
    write_active_flights,
    write_config,
    write_flight_state,
)

BYAIR_CAL = "c_byair@group.calendar.google.com"
FIXED_NOW = datetime(2026, 7, 1, 13, 0, 0, tzinfo=timezone.utc)  # before all fixture flights arrive


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "state" / "flight-assist"
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(root))
    return root


class FakeCalendar:
    """Records every call; returns controlled list/find/create responses.

    Speaks the native shapes `GoogleCalendarClient` returns: `items` for both
    list surfaces, the created event resource for a create.

    `delete_error` (a GoogleCalendarError) is raised on delete to exercise the
    404-idempotent path and the non-404 failure path.
    """

    def __init__(
        self,
        *,
        calendars: list[dict] | None = None,
        events_by_calendar: dict[str, list[dict]] | None = None,
        create_id: str = "evt_created",
        delete_error: GoogleCalendarError | None = None,
    ):
        self.calendars = calendars or []
        self.events_by_calendar = events_by_calendar or {}
        self.create_id = create_id
        self.delete_error = delete_error
        self.calls: list[tuple[str, dict]] = []

    def list_calendars(self, arguments: dict | None = None) -> dict:
        self.calls.append(("list_calendars", arguments if arguments is not None else {}))
        # calendarList.list returns the calendars under `items`.
        return {"items": self.calendars}

    def find_events(self, arguments: dict) -> dict:
        self.calls.append(("find_events", arguments))
        # events.list returns the events under `items`; the client merges every
        # page into that same shape.
        events = self.events_by_calendar.get(arguments["calendar_id"], [])
        return {"items": events}

    def create_event(self, arguments: dict) -> dict:
        self.calls.append(("create_event", arguments))
        return {"id": self.create_id}

    def patch_event(self, arguments: dict) -> dict:
        self.calls.append(("patch_event", arguments))
        return {"id": arguments.get("event_id")}

    def delete_event(self, arguments: dict) -> dict:
        self.calls.append(("delete_event", arguments))
        if self.delete_error is not None:
            raise self.delete_error
        return {}

    def calls_named(self, name: str) -> list[dict]:
        return [args for called, args in self.calls if called == name]


def _phase_markers() -> dict:
    return {
        "day_before_fired": False,
        "time_to_leave_fired": False,
        "boarding_fired": False,
        "arrival_logistics_fired": False,
        "landed_acknowledged": False,
        "connection_at_risk_fired": False,
        "gate_assignment_fired": False,
    }


def _write_flight(
    *,
    flight_id: int = 1,
    code: str = "AA100",
    trip_id: int = 10,
    dep_airport_id: int = 20,
    arr_airport_id: int = 28,
    scheduled_dep: str = "2026-07-01T10:00:00-05:00",
    scheduled_arr: str = "2026-07-01T12:30:00-05:00",
    last_snapshot: dict | None = None,
    calendar_events: dict | None = None,
) -> None:
    state = {
        "flight_id": flight_id,
        "code": code,
        "ownership": "mine",
        "trip_id": trip_id,
        "scheduled_dep_time": scheduled_dep,
        "scheduled_arr_time": scheduled_arr,
        "dep_airport_id": dep_airport_id,
        "arr_airport_id": arr_airport_id,
        "last_polled_at": "2026-07-01T12:00:00Z",
        "phase_markers": _phase_markers(),
    }
    if last_snapshot is not None:
        state["last_snapshot"] = last_snapshot
    if calendar_events is not None:
        state["calendar_events"] = calendar_events
    write_flight_state(state)


def _raw_event(
    *,
    event_id: str,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    private: dict | None = None,
) -> dict:
    """A Google-native raw event resource (normalize_event input shape).

    A managed event's tags ride in the description's <!--fa:{...}--> comment,
    so `private` encodes into the description exactly as the reconcile writes
    it.
    """
    raw: dict = {
        "id": event_id,
        "summary": summary,
        "description": encode_tags(description, private) if private else description,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    return raw


# --- write-arg shapes (native events.insert / events.patch bodies) ----------


def test_create_event_args_are_native_with_tags_in_extended_properties():
    # #193 writer flip: tags live in extendedProperties.private; the description
    # carries only the human content (tag-free).
    op = {
        "calendar_id": BYAIR_CAL,
        "body": {
            "summary": "Boarding AA100",
            "start": "2026-07-01T09:30:00-05:00",
            "end": "2026-07-01T10:00:00-05:00",
            "private_props": {"faFlightId": "1", "faKind": "boarding"},
        },
    }
    args = cr._create_event_args(op)
    assert args["start"] == {"dateTime": "2026-07-01T09:30:00-05:00"}
    assert args["end"] == {"dateTime": "2026-07-01T10:00:00-05:00"}
    assert "start_datetime" not in args
    assert "event_duration_hour" not in args and "event_duration_minutes" not in args
    assert args["extendedProperties"] == {"private": {"faFlightId": "1", "faKind": "boarding"}}
    assert "<!--fa:" not in args["description"]


def test_create_event_args_send_no_timezone():
    """The offset in `dateTime` IS the instant — Calendar reads it verbatim.

    This is the #82/#83 regression guard, inverted. Composio's adapter ignored
    the offset and re-read the wall-clock as UTC, so the create had to carry a
    synthesized `Etc/GMT±N` zone to land at the right time. Sending that zone
    now would pin the event to a fixed offset for no reason; sending nothing is
    both correct and simpler.
    """
    op = {
        "calendar_id": BYAIR_CAL,
        "body": {
            "summary": "Boarding AA100",
            "start": "2026-07-01T09:30:00-05:00",
            "end": "2026-07-01T10:00:00-05:00",
            "private_props": {},
        },
    }
    args = cr._create_event_args(op)
    assert "timeZone" not in args["start"] and "timeZone" not in args["end"]
    assert "timezone" not in args


def test_create_event_args_preserve_non_whole_hour_offset():
    """+05:30 (India) had no Etc zone, so the old adapter normalized the whole
    timestamp to UTC to keep the instant right. Natively the offset is simply
    honoured, so the caller's own string survives."""
    op = {
        "calendar_id": BYAIR_CAL,
        "body": {
            "summary": "Boarding AI101",
            "start": "2026-07-01T09:30:00+05:30",
            "end": "2026-07-01T10:00:00+05:30",
            "private_props": {},
        },
    }
    args = cr._create_event_args(op)
    assert args["start"] == {"dateTime": "2026-07-01T09:30:00+05:30"}


def test_patch_event_args_delta_shift_uses_native_times():
    op = {
        "calendar_id": BYAIR_CAL,
        "event_id": "e1",
        "body": {"start": "2026-07-01T10:15:00-05:00", "end": "2026-07-01T12:45:00-05:00"},
    }
    args = cr._patch_event_args(op)
    assert args["start"] == {"dateTime": "2026-07-01T10:15:00-05:00"}
    assert args["end"] == {"dateTime": "2026-07-01T12:45:00-05:00"}
    assert "start_time" not in args and "extendedProperties" not in args


def test_patch_event_args_adopt_writes_tags_to_extended_properties():
    # #193 writer flip: an adopt writes tags to extendedProperties.private and
    # leaves byAir's description tag-free (never clobbered with a tag comment).
    op = {
        "calendar_id": BYAIR_CAL,
        "event_id": "e1",
        "body": {
            "description": "✈ BNA→YYZ • UA 8018",
            "private_props": {"faFlightId": "1", "faKind": "flight", "faManaged": "adopted"},
        },
    }
    args = cr._patch_event_args(op)
    assert args["extendedProperties"] == {
        "private": {"faFlightId": "1", "faKind": "flight", "faManaged": "adopted"}
    }
    # byAir's description is preserved, tag-free.
    assert args["description"] == "✈ BNA→YYZ • UA 8018"
    assert "<!--fa:" not in args["description"]
    assert "start" not in args


def test_patch_event_args_adopt_strips_legacy_description_tag():
    # A pre-flip adopted event carries a <!--fa:--> comment in its description;
    # the flipped adopt migrates it — strips the comment, writes extendedProperties.
    op = {
        "calendar_id": BYAIR_CAL,
        "event_id": "e1",
        "body": {
            "description": encode_tags("✈ BNA→YYZ • UA 8018", {"faFlightId": "1"}),
            "private_props": {"faFlightId": "1", "faKind": "flight", "faManaged": "adopted"},
        },
    }
    args = cr._patch_event_args(op)
    assert args["description"] == "✈ BNA→YYZ • UA 8018"
    assert "<!--fa:" not in args["description"]
    assert args["extendedProperties"]["private"]["faManaged"] == "adopted"


def test_items_reads_the_native_items_list():
    """events.list and calendarList.list both put their rows in `items`, and
    the client merges its pages into that same shape — so there is one key to
    read, not the four container shapes Composio could return."""
    assert cr._items({"items": [{"id": "x"}]}) == [{"id": "x"}]
    assert cr._items({"items": []}) == []
    assert cr._items({}) == []
    assert cr._items({"items": "not a list"}) == []
    assert cr._items("not a dict") == []


# --- calendar-ID resolution --------------------------------------------------


def test_resolve_uses_cached_id_without_listing(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    client = FakeCalendar()
    resolved = cr.resolve_byair_calendar_id(client, must(read_config()))
    assert resolved == BYAIR_CAL
    assert client.calls_named("list_calendars") == []  # cached → no lookup


def test_resolve_by_name_matches_and_caches(state_root: Path):
    write_config({"byair_calendar_name": "Flighty Flights"})
    client = FakeCalendar(
        calendars=[
            {"id": "c_other@g", "summary": "Work"},
            {"id": BYAIR_CAL, "summary": "Flighty Flights"},
        ]
    )
    resolved = cr.resolve_byair_calendar_id(client, must(read_config()))
    assert resolved == BYAIR_CAL
    # Cached back to config so later cycles skip the lookup.
    assert must(read_config())["byair_calendar_id"] == BYAIR_CAL


def test_resolve_by_name_is_case_and_whitespace_insensitive(state_root: Path):
    write_config({"byair_calendar_name": "  flighty flights "})
    client = FakeCalendar(calendars=[{"id": BYAIR_CAL, "summary": "Flighty Flights"}])
    assert cr.resolve_byair_calendar_id(client, must(read_config())) == BYAIR_CAL


def test_resolve_by_name_no_match_returns_none(state_root: Path):
    write_config({"byair_calendar_name": "Nonexistent"})
    client = FakeCalendar(calendars=[{"id": BYAIR_CAL, "summary": "Flighty Flights"}])
    assert cr.resolve_byair_calendar_id(client, must(read_config())) is None
    assert "byair_calendar_id" not in must(read_config())  # nothing cached on a miss


def test_resolve_no_config_returns_none(state_root: Path):
    write_config({"home_address": "1 Loop"})
    client = FakeCalendar()
    assert cr.resolve_byair_calendar_id(client, must(read_config())) is None
    assert client.calls_named("list_calendars") == []


# --- run_reconcile status guards --------------------------------------------


def test_run_reconcile_no_calendar_when_unconfigured(state_root: Path):
    write_config({"home_address": "1 Loop"})
    summary = cr.run_reconcile(FakeCalendar(), now=FIXED_NOW)
    assert summary["status"] == "no_calendar"
    assert summary["executed"] == 0


def test_run_reconcile_no_flights_when_index_empty(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    write_active_flights([])
    summary = cr.run_reconcile(FakeCalendar(), now=FIXED_NOW)
    assert summary["status"] == "no_flights"
    assert summary["executed"] == 0


# --- create / adopt / delete end-to-end -------------------------------------


def test_creates_boarding_block_and_writes_ledger(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    _write_flight(flight_id=1)  # empty ledger, no snapshot → effective = scheduled
    write_active_flights([1])
    client = FakeCalendar(create_id="evt_boarding_new")  # no events on any calendar

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    assert summary["status"] == "ok"
    created = client.calls_named("create_event")
    assert len(created) == 1
    assert created[0]["calendar_id"] == BYAIR_CAL
    ledger = must(read_flight_state(1))["calendar_events"]
    assert ledger["boarding"]["event_id"] == "evt_boarding_new"
    assert ledger["boarding"]["managed"] == "created"
    assert ledger["boarding"]["synced_signature"]  # a non-empty <start>/<end>


def test_adopts_byair_flight_event_and_tags_it(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    # Pre-seed a synced boarding entry + matching live boarding event so the
    # boarding plan no-ops and the adopt is the only flight-event op. Ledger
    # signatures are stored in the planner's normalized-UTC form.
    boarding_sig = _sig("2026-07-01T09:30:00-05:00", "2026-07-01T10:00:00-05:00")
    flight_sig = _sig("2026-07-01T10:00:00-05:00", "2026-07-01T12:30:00-05:00")
    _write_flight(
        flight_id=1,
        calendar_events={
            "boarding": {
                "event_id": "evt_boarding_1",
                "calendar_id": BYAIR_CAL,
                "managed": "created",
                "synced_signature": boarding_sig,
            }
        },
    )
    write_active_flights([1])
    byair_flight_event = _raw_event(
        event_id="byair_flight_evt",
        summary="AA 100 Nashville → Baltimore",
        start="2026-07-01T10:00:00-05:00",
        end="2026-07-01T12:30:00-05:00",
    )
    live_boarding = _raw_event(
        event_id="evt_boarding_1",
        summary="Boarding AA100",
        start="2026-07-01T09:30:00-05:00",
        end="2026-07-01T10:00:00-05:00",
        private={"faFlightId": "1", "faKind": "boarding", "faManaged": "created"},
    )
    client = FakeCalendar(events_by_calendar={BYAIR_CAL: [byair_flight_event, live_boarding]})

    cr.run_reconcile(client, now=FIXED_NOW)

    patched = client.calls_named("patch_event")
    assert any(a["event_id"] == "byair_flight_evt" for a in patched)
    ledger = must(read_flight_state(1))["calendar_events"]
    assert ledger["flight"]["event_id"] == "byair_flight_evt"
    assert ledger["flight"]["managed"] == "adopted"
    assert ledger["flight"]["synced_signature"] == flight_sig


def test_teardown_deletes_managed_events_on_cancel(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    _write_flight(
        flight_id=1,
        last_snapshot={"computed_status": "cancelled"},
        calendar_events={
            "boarding": {
                "event_id": "evt_boarding_1",
                "calendar_id": BYAIR_CAL,
                "managed": "created",
                "synced_signature": "s",
            }
        },
    )
    write_active_flights([1])
    client = FakeCalendar()

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    deleted = client.calls_named("delete_event")
    assert any(a["event_id"] == "evt_boarding_1" for a in deleted)
    assert "boarding" not in must(read_flight_state(1))["calendar_events"]
    assert summary["failed"] == []


def test_delete_404_is_idempotent_success(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    _write_flight(
        flight_id=1,
        last_snapshot={"computed_status": "cancelled"},
        calendar_events={
            "boarding": {
                "event_id": "gone",
                "calendar_id": BYAIR_CAL,
                "managed": "created",
                "synced_signature": "s",
            }
        },
    )
    write_active_flights([1])
    client = FakeCalendar(delete_error=GoogleCalendarError("not found", status_code=404))

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    # 404 = already gone → success: ledger entry dropped, no failure recorded.
    assert "boarding" not in must(read_flight_state(1))["calendar_events"]
    assert summary["failed"] == []


def test_non_404_delete_failure_is_collected_and_ledger_kept(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    entry = {
        "event_id": "evt_boarding_1",
        "calendar_id": BYAIR_CAL,
        "managed": "created",
        "synced_signature": "s",
    }
    _write_flight(
        flight_id=1,
        last_snapshot={"computed_status": "cancelled"},
        calendar_events={"boarding": dict(entry)},
    )
    write_active_flights([1])
    client = FakeCalendar(delete_error=GoogleCalendarError("server error", status_code=500))

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    # A real failure is collected, not raised; the ledger entry is retained so
    # the next cycle retries the delete.
    assert len(summary["failed"]) == 1
    assert summary["failed"][0]["op"] == "delete"
    assert must(read_flight_state(1))["calendar_events"]["boarding"] == entry


def test_deletes_reclaim_travel_block_in_same_airport_gap(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    # Two legs of one trip, same connecting airport (28) → the layover gap has
    # no ground transfer, so a Reclaim travel block in it is bogus.
    _write_flight(
        flight_id=1,
        code="AA100",
        trip_id=10,
        dep_airport_id=20,
        arr_airport_id=28,
        scheduled_dep="2026-07-01T08:00:00-05:00",
        scheduled_arr="2026-07-01T10:00:00-05:00",
        calendar_events={
            "boarding": {
                "event_id": "b1",
                "calendar_id": BYAIR_CAL,
                "managed": "created",
                "synced_signature": _sig("2026-07-01T07:30:00-05:00", "2026-07-01T08:00:00-05:00"),
            }
        },
    )
    _write_flight(
        flight_id=2,
        code="AA200",
        trip_id=10,
        dep_airport_id=28,
        arr_airport_id=40,
        scheduled_dep="2026-07-01T12:00:00-05:00",
        scheduled_arr="2026-07-01T14:00:00-05:00",
        calendar_events={
            "boarding": {
                "event_id": "b2",
                "calendar_id": BYAIR_CAL,
                "managed": "created",
                "synced_signature": _sig("2026-07-01T11:30:00-05:00", "2026-07-01T12:00:00-05:00"),
            }
        },
    )
    write_active_flights([1, 2])
    reclaim_block = _raw_event(
        event_id="reclaim_travel_1",
        summary="🚌 Travel",
        start="2026-07-01T10:15:00-05:00",  # inside the [10:00, 12:00] same-airport gap
        end="2026-07-01T10:45:00-05:00",
        description="Auto-scheduled by <a href='https://app.reclaim.ai/x'>Reclaim</a>",
    )
    # Pre-seed live boarding events matching the ledger signatures so the two
    # boarding plans no-op and the Reclaim delete is the op under test.
    live_b1 = _raw_event(
        event_id="b1",
        summary="Boarding AA100",
        start="2026-07-01T07:30:00-05:00",
        end="2026-07-01T08:00:00-05:00",
        private={"faFlightId": "1", "faKind": "boarding", "faManaged": "created"},
    )
    live_b2 = _raw_event(
        event_id="b2",
        summary="Boarding AA200",
        start="2026-07-01T11:30:00-05:00",
        end="2026-07-01T12:00:00-05:00",
        private={"faFlightId": "2", "faKind": "boarding", "faManaged": "created"},
    )
    client = FakeCalendar(
        events_by_calendar={
            BYAIR_CAL: [live_b1, live_b2],
            cr.PRIMARY_CALENDAR_ID: [reclaim_block],
        }
    )

    cr.run_reconcile(client, now=datetime(2026, 7, 1, 12, 30, 0, tzinfo=timezone.utc))

    deleted = client.calls_named("delete_event")
    assert [a["event_id"] for a in deleted] == ["reclaim_travel_1"]
    assert deleted[0]["calendar_id"] == cr.PRIMARY_CALENDAR_ID


def test_delta_only_noop_when_already_synced(state_root: Path):
    write_config({"byair_calendar_id": BYAIR_CAL})
    boarding_sig = _sig("2026-07-01T09:30:00-05:00", "2026-07-01T10:00:00-05:00")
    flight_sig = _sig("2026-07-01T10:00:00-05:00", "2026-07-01T12:30:00-05:00")
    _write_flight(
        flight_id=1,
        calendar_events={
            "boarding": {
                "event_id": "evt_boarding_1",
                "calendar_id": BYAIR_CAL,
                "managed": "created",
                "synced_signature": boarding_sig,
            },
            "flight": {
                "event_id": "evt_flight_1",
                "calendar_id": BYAIR_CAL,
                "managed": "adopted",
                "synced_signature": flight_sig,
            },
        },
    )
    write_active_flights([1])
    live_boarding = _raw_event(
        event_id="evt_boarding_1",
        summary="Boarding AA100",
        start="2026-07-01T09:30:00-05:00",
        end="2026-07-01T10:00:00-05:00",
        private={"faFlightId": "1", "faKind": "boarding", "faManaged": "created"},
    )
    live_flight = _raw_event(
        event_id="evt_flight_1",
        summary="AA 100",
        start="2026-07-01T10:00:00-05:00",
        end="2026-07-01T12:30:00-05:00",
        private={"faFlightId": "1", "faKind": "flight", "faManaged": "adopted"},
    )
    client = FakeCalendar(events_by_calendar={BYAIR_CAL: [live_boarding, live_flight]})

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    assert summary["planned"] == 0
    assert summary["executed"] == 0
    assert client.calls_named("create_event") == []
    assert client.calls_named("patch_event") == []
    assert client.calls_named("delete_event") == []


# --- tombstone sweep + archival ----------------------------------------------


def _ledger(*, boarding: bool = False, flight: bool = False) -> dict:
    led: dict = {}
    if boarding:
        led["boarding"] = {
            "event_id": "evt_b",
            "calendar_id": BYAIR_CAL,
            "managed": "created",
            "synced_signature": "s",
        }
    if flight:
        led["flight"] = {
            "event_id": "evt_f",
            "calendar_id": BYAIR_CAL,
            "managed": "adopted",
            "synced_signature": "s",
        }
    return led


def test_tombstone_sweep_tears_down_switched_away_flight(state_root: Path):
    """A flight gone from active-flights but still future (switched away) and
    still carrying a ledger has its managed events deleted and its state file
    archived — even though it is not in the active index. With no active
    flights, no calendar fetch is needed (teardown is ledger-driven)."""
    write_config({"byair_calendar_id": BYAIR_CAL})
    _write_flight(flight_id=1, calendar_events=_ledger(boarding=True, flight=True))
    write_active_flights([])  # flight 1 dropped from the index
    client = FakeCalendar()

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    deleted = {a["event_id"] for a in client.calls_named("delete_event")}
    assert deleted == {"evt_b", "evt_f"}
    assert client.calls_named("find_events") == []  # no active flights → no fetch
    assert cr.read_flight_state(1) is None  # archived once teardown settled
    assert summary["status"] == "ok"
    assert summary["archived"] == 1
    assert summary["failed"] == []


def test_tombstone_sweep_archives_completed_flight_without_touching_calendar(state_root: Path):
    """A completed flight out of active-flights leaves its managed events as a
    historical record (no deletes), but the state file is archived — we stop
    tracking a flight that is done and gone from the index."""
    write_config({"byair_calendar_id": BYAIR_CAL})
    _write_flight(
        flight_id=1,
        scheduled_dep="2026-07-01T06:00:00-05:00",
        scheduled_arr="2026-07-01T07:00:00-05:00",  # 12:00Z, before FIXED_NOW → completed
        calendar_events=_ledger(boarding=True),
    )
    write_active_flights([])
    client = FakeCalendar()

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    assert client.calls_named("delete_event") == []  # events kept as a record
    assert cr.read_flight_state(1) is None  # but the tombstone is archived
    assert summary["archived"] == 1


def test_tombstone_retained_when_teardown_delete_fails(state_root: Path):
    """A failed teardown delete keeps the ledger entry AND the state file — the
    sweep retries next cycle rather than archiving with events still live."""
    write_config({"byair_calendar_id": BYAIR_CAL})
    entry = dict(_ledger(boarding=True)["boarding"])
    _write_flight(flight_id=1, calendar_events={"boarding": dict(entry)})
    write_active_flights([])
    client = FakeCalendar(delete_error=GoogleCalendarError("server error", status_code=500))

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    assert len(summary["failed"]) == 1
    assert summary["archived"] == 0
    retained = cr.read_flight_state(1)
    assert retained is not None
    assert retained["calendar_events"]["boarding"] == entry


def test_non_active_flight_without_ledger_is_not_a_tombstone(state_root: Path):
    """An on-disk flight out of active-flights with no ledger has nothing to
    tear down — it is not swept, archived, or otherwise touched."""
    write_config({"byair_calendar_id": BYAIR_CAL})
    _write_flight(flight_id=2)  # on disk, not active, no calendar_events
    write_active_flights([])
    client = FakeCalendar()

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    assert summary["status"] == "no_flights"
    assert cr.read_flight_state(2) is not None  # untouched
    assert client.calls_named("delete_event") == []


def test_active_and_tombstone_reconciled_in_one_cycle(state_root: Path):
    """The active pass (fetch + reconcile) and the tombstone sweep (ledger
    teardown + archive) both run in a single cycle. The fetch window is built
    from active flights only, so a far-off tombstone never widens it."""
    write_config({"byair_calendar_id": BYAIR_CAL})
    _write_flight(flight_id=1)  # active, needs a boarding block created
    _write_flight(flight_id=2, calendar_events=_ledger(boarding=True))  # switched-away tombstone
    write_active_flights([1])
    client = FakeCalendar()

    summary = cr.run_reconcile(client, now=FIXED_NOW)

    assert client.calls_named("find_events")  # active pass fetched live events
    deleted = {a["event_id"] for a in client.calls_named("delete_event")}
    assert "evt_b" in deleted  # tombstone torn down
    assert cr.read_flight_state(2) is None  # tombstone archived
    assert cr.read_flight_state(1) is not None  # active flight retained
    assert summary["archived"] == 1


# --- helper-level coverage ---------------------------------------------------


def test_effective_times_prefers_actual_snapshot_times():
    state = {
        "scheduled_dep_time": "2026-07-01T10:00:00-05:00",
        "scheduled_arr_time": "2026-07-01T12:30:00-05:00",
        "last_snapshot": {
            "dep_time": "2026-07-01T10:45:00-05:00",  # byAir delayed it
            "arr_time": "2026-07-01T13:10:00-05:00",
        },
    }
    assert cr._effective_times(state) == (
        "2026-07-01T10:45:00-05:00",
        "2026-07-01T13:10:00-05:00",
    )


def test_effective_times_falls_back_to_scheduled_without_snapshot():
    state = {
        "scheduled_dep_time": "2026-07-01T10:00:00-05:00",
        "scheduled_arr_time": "2026-07-01T12:30:00-05:00",
    }
    assert cr._effective_times(state) == (
        "2026-07-01T10:00:00-05:00",
        "2026-07-01T12:30:00-05:00",
    )


def test_collect_events_filters_all_day_and_keeps_timed(state_root: Path):
    all_day = {
        "id": "holiday",
        "summary": "Vacation",
        "start": {"date": "2026-07-01"},
        "end": {"date": "2026-07-02"},
    }
    timed = _raw_event(
        event_id="timed_1",
        summary="AA100",
        start="2026-07-01T10:00:00-05:00",
        end="2026-07-01T12:30:00-05:00",
    )
    client = FakeCalendar(
        events_by_calendar={BYAIR_CAL: [all_day, timed], cr.PRIMARY_CALENDAR_ID: []}
    )
    events = cr.collect_events(
        client,
        byair_calendar_id=BYAIR_CAL,
        time_min="2026-07-01T00:00:00Z",
        time_max="2026-07-02T00:00:00Z",
    )
    ids = [e["event_id"] for e in events]
    assert ids == ["timed_1"]


def test_collect_events_skips_event_without_id(state_root: Path):
    no_id = {
        "summary": "broken",
        "start": {"dateTime": "2026-07-01T10:00:00-05:00"},
        "end": {"dateTime": "2026-07-01T11:00:00-05:00"},
    }
    client = FakeCalendar(events_by_calendar={BYAIR_CAL: [no_id], cr.PRIMARY_CALENDAR_ID: []})
    events = cr.collect_events(
        client,
        byair_calendar_id=BYAIR_CAL,
        time_min="2026-07-01T00:00:00Z",
        time_max="2026-07-02T00:00:00Z",
    )
    assert events == []
