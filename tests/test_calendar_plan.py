"""Tests for the pure calendar reconciliation planner (`calendar_plan.py`).

Deterministic fixtures only. Each test builds minimal flight/event inputs
and asserts the op list the planner emits — outcomes, not internals.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from calendar_plan import (  # noqa: E402
    KIND_BOARDING,
    KIND_FLIGHT,
    KIND_RECLAIM_TRAVEL,
    MANAGED_ADOPTED,
    MANAGED_CREATED,
    TAG_FLIGHT_ID,
    TAG_KIND,
    TAG_MANAGED,
    plan_reconciliation,
)

BYAIR_CAL = "c_byair@group.calendar.google.com"
RECLAIM_CAL = "c_reclaim@group.calendar.google.com"


def _config():
    return {
        "byair_calendar_id": BYAIR_CAL,
        "reclaim_calendar_id": RECLAIM_CAL,
        "boarding_calendar_id": BYAIR_CAL,
    }


def _flight(
    *,
    flight_id=1,
    code="AA100",
    trip_id=10,
    dep_airport_id=20,
    arr_airport_id=28,
    dep_time="2026-07-01T10:00:00-05:00",
    arr_time="2026-07-01T12:30:00-05:00",
    boarding_lead_minutes=30,
    disposition="active",
    calendar_events=None,
):
    return {
        "flight_id": flight_id,
        "code": code,
        "trip_id": trip_id,
        "dep_airport_id": dep_airport_id,
        "arr_airport_id": arr_airport_id,
        "dep_time": dep_time,
        "arr_time": arr_time,
        "boarding_lead_minutes": boarding_lead_minutes,
        "disposition": disposition,
        "calendar_events": calendar_events or {},
    }


def _event(
    *,
    event_id="e1",
    calendar_id=BYAIR_CAL,
    summary="AA100 Nashville to Baltimore",
    start="2026-07-01T10:00:00-05:00",
    end="2026-07-01T12:30:00-05:00",
    private_props=None,
    is_reclaim_travel=False,
):
    return {
        "event_id": event_id,
        "calendar_id": calendar_id,
        "summary": summary,
        "start": start,
        "end": end,
        "private_props": private_props or {},
        "is_reclaim_travel": is_reclaim_travel,
    }


def _ops_of(ops, *, op=None, kind=None):
    return [o for o in ops if (op is None or o["op"] == op) and (kind is None or o["kind"] == kind)]


# --- boarding block ----------------------------------------------------


def test_boarding_created_when_untracked():
    ops = plan_reconciliation([_flight()], [], _config())
    created = _ops_of(ops, op="create", kind=KIND_BOARDING)
    assert len(created) == 1
    body = created[0]["body"]
    # 30-min lead before 10:00-05:00 departure.
    assert body["start"] == "2026-07-01T09:30:00-05:00"
    assert body["end"] == "2026-07-01T10:00:00-05:00"
    assert body["private_props"][TAG_FLIGHT_ID] == "1"
    assert body["private_props"][TAG_KIND] == KIND_BOARDING
    assert body["private_props"][TAG_MANAGED] == MANAGED_CREATED
    assert created[0]["calendar_id"] == BYAIR_CAL


def test_boarding_lead_50_widebody_window():
    ops = plan_reconciliation([_flight(boarding_lead_minutes=50)], [], _config())
    body = _ops_of(ops, op="create", kind=KIND_BOARDING)[0]["body"]
    assert body["start"] == "2026-07-01T09:10:00-05:00"  # 50 min before 10:00


def test_boarding_noop_when_live_matches_signature():
    boarding_event = _event(
        event_id="b1",
        start="2026-07-01T09:30:00-05:00",
        end="2026-07-01T10:00:00-05:00",
        private_props={TAG_FLIGHT_ID: "1", TAG_KIND: KIND_BOARDING},
    )
    ledger = {
        KIND_BOARDING: {
            "event_id": "b1",
            "calendar_id": BYAIR_CAL,
            "managed": MANAGED_CREATED,
            "synced_signature": "2026-07-01T14:30:00Z/2026-07-01T15:00:00Z",
        }
    }
    ops = plan_reconciliation([_flight(calendar_events=ledger)], [boarding_event], _config())
    assert _ops_of(ops, kind=KIND_BOARDING) == []


def test_boarding_updated_when_departure_shifts():
    # Live boarding block sits at the old time; flight now departs an hour later.
    boarding_event = _event(
        event_id="b1",
        start="2026-07-01T09:30:00-05:00",
        end="2026-07-01T10:00:00-05:00",
    )
    ledger = {
        KIND_BOARDING: {
            "event_id": "b1",
            "calendar_id": BYAIR_CAL,
            "managed": MANAGED_CREATED,
            "synced_signature": "2026-07-01T14:30:00Z/2026-07-01T15:00:00Z",
        }
    }
    flight = _flight(
        dep_time="2026-07-01T11:00:00-05:00",
        arr_time="2026-07-01T13:30:00-05:00",
        calendar_events=ledger,
    )
    ops = plan_reconciliation([flight], [boarding_event], _config())
    updated = _ops_of(ops, op="update", kind=KIND_BOARDING)
    assert len(updated) == 1
    assert updated[0]["event_id"] == "b1"
    assert updated[0]["body"]["start"] == "2026-07-01T10:30:00-05:00"


def test_boarding_recreated_when_tracked_event_missing():
    ledger = {
        KIND_BOARDING: {
            "event_id": "gone",
            "calendar_id": BYAIR_CAL,
            "managed": MANAGED_CREATED,
            "synced_signature": "2026-07-01T14:30:00Z/2026-07-01T15:00:00Z",
        }
    }
    ops = plan_reconciliation([_flight(calendar_events=ledger)], [], _config())
    assert len(_ops_of(ops, op="create", kind=KIND_BOARDING)) == 1


# --- byAir flight event (adopt + shift) --------------------------------


def test_flight_event_adopted_by_match():
    byair_event = _event(event_id="f1", summary="AA100 to Baltimore")
    ops = plan_reconciliation([_flight()], [byair_event], _config())
    adopt = _ops_of(ops, op="adopt", kind=KIND_FLIGHT)
    assert len(adopt) == 1
    assert adopt[0]["event_id"] == "f1"
    assert adopt[0]["body"]["private_props"][TAG_MANAGED] == MANAGED_ADOPTED
    assert adopt[0]["body"]["private_props"][TAG_FLIGHT_ID] == "1"


def test_flight_event_adopted_despite_spaced_code_in_summary():
    # Real Flighty summaries render the code with a space ("UA 8018") while
    # byAir's code field carries it unspaced ("UA8018"); the match is
    # whitespace-insensitive so adoption still fires. Summary uses the real
    # Flighty format including the zero-width spaces around the arrow.
    byair_event = _event(event_id="f1", summary="✈ BNA​→​YYZ • UA 8018")
    flight = _flight(code="UA8018")
    ops = plan_reconciliation([flight], [byair_event], _config())
    adopt = _ops_of(ops, op="adopt", kind=KIND_FLIGHT)
    assert len(adopt) == 1
    assert adopt[0]["event_id"] == "f1"


def test_flight_event_adopted_when_code_itself_has_space():
    # byAir's code field may itself carry the space; still matches.
    byair_event = _event(event_id="f1", summary="✈ BNA→YYZ • UA8018")
    flight = _flight(code="UA 8018")
    ops = plan_reconciliation([flight], [byair_event], _config())
    assert len(_ops_of(ops, op="adopt", kind=KIND_FLIGHT)) == 1


def test_flight_event_not_adopted_when_already_tagged():
    tagged = _event(event_id="f1", private_props={TAG_FLIGHT_ID: "1"})
    ops = plan_reconciliation([_flight()], [tagged], _config())
    assert _ops_of(ops, kind=KIND_FLIGHT) == []


def test_flight_event_not_adopted_when_outside_tolerance():
    # Same code but start is days away — not the same leg.
    far = _event(
        event_id="f1",
        start="2026-07-05T10:00:00-05:00",
        end="2026-07-05T12:30:00-05:00",
    )
    ops = plan_reconciliation([_flight()], [far], _config())
    assert _ops_of(ops, kind=KIND_FLIGHT) == []


def test_flight_event_noop_when_byair_already_shifted():
    # Adopted event already sits at the actual times -> no shift needed.
    ledger = {
        KIND_FLIGHT: {
            "event_id": "f1",
            "calendar_id": BYAIR_CAL,
            "managed": MANAGED_ADOPTED,
            "synced_signature": "2026-07-01T15:00:00Z/2026-07-01T17:30:00Z",
        }
    }
    live = _event(event_id="f1")  # start/end == actual dep/arr
    ops = plan_reconciliation([_flight(calendar_events=ledger)], [live], _config())
    assert _ops_of(ops, kind=KIND_FLIGHT) == []


def test_flight_event_shifted_when_byair_left_it_stale():
    ledger = {
        KIND_FLIGHT: {
            "event_id": "f1",
            "calendar_id": BYAIR_CAL,
            "managed": MANAGED_ADOPTED,
            "synced_signature": "2026-07-01T15:00:00Z/2026-07-01T17:30:00Z",
        }
    }
    stale = _event(event_id="f1")  # still at old time
    flight = _flight(
        dep_time="2026-07-01T11:00:00-05:00",
        arr_time="2026-07-01T13:30:00-05:00",
        calendar_events=ledger,
    )
    ops = plan_reconciliation([flight], [stale], _config())
    shift = _ops_of(ops, op="update", kind=KIND_FLIGHT)
    assert len(shift) == 1
    assert shift[0]["body"]["start"] == "2026-07-01T11:00:00-05:00"


def test_flight_event_forgotten_when_vanished():
    ledger = {
        KIND_FLIGHT: {
            "event_id": "f1",
            "calendar_id": BYAIR_CAL,
            "managed": MANAGED_ADOPTED,
            "synced_signature": "2026-07-01T15:00:00Z/2026-07-01T17:30:00Z",
        }
    }
    ops = plan_reconciliation([_flight(calendar_events=ledger)], [], _config())
    forget = _ops_of(ops, op="forget", kind=KIND_FLIGHT)
    assert len(forget) == 1
    assert forget[0]["event_id"] == "f1"


# --- Reclaim positional deletion ---------------------------------------


def _two_leg_trip(*, same_airport: bool):
    leg1 = _flight(
        flight_id=1,
        code="AA100",
        dep_airport_id=20,
        arr_airport_id=30,
        dep_time="2026-07-01T08:00:00-05:00",
        arr_time="2026-07-01T10:00:00-05:00",
    )
    leg2 = _flight(
        flight_id=2,
        code="AA200",
        dep_airport_id=30 if same_airport else 31,
        arr_airport_id=40,
        dep_time="2026-07-01T13:00:00-05:00",
        arr_time="2026-07-01T16:00:00-05:00",
    )
    return [leg1, leg2]


def _reclaim_gap_event():
    # A Reclaim travel block sitting inside the 10:00->13:00 layover gap.
    return _event(
        event_id="r1",
        calendar_id=RECLAIM_CAL,
        summary="Travel",
        start="2026-07-01T11:30:00-05:00",
        end="2026-07-01T12:00:00-05:00",
        is_reclaim_travel=True,
    )


def test_reclaim_deleted_in_same_airport_gap():
    ops = plan_reconciliation(_two_leg_trip(same_airport=True), [_reclaim_gap_event()], _config())
    deleted = _ops_of(ops, op="delete", kind=KIND_RECLAIM_TRAVEL)
    assert len(deleted) == 1
    assert deleted[0]["event_id"] == "r1"


def test_reclaim_kept_when_airports_differ():
    ops = plan_reconciliation(_two_leg_trip(same_airport=False), [_reclaim_gap_event()], _config())
    assert _ops_of(ops, kind=KIND_RECLAIM_TRAVEL) == []


def test_reclaim_rule_ignores_non_reclaim_calendar():
    # Same window, but the event is on the byAir calendar, not Reclaim's.
    user_event = _event(
        event_id="u1",
        calendar_id=BYAIR_CAL,
        start="2026-07-01T11:30:00-05:00",
        end="2026-07-01T12:00:00-05:00",
    )
    ops = plan_reconciliation(_two_leg_trip(same_airport=True), [user_event], _config())
    assert _ops_of(ops, kind=KIND_RECLAIM_TRAVEL) == []


def test_reclaim_rule_spares_non_travel_block_on_reclaim_calendar():
    # A Reclaim focus/habit block in the gap (not is_reclaim_travel) is kept:
    # the Reclaim calendar holds more than travel blocks.
    focus_block = _event(
        event_id="focus1",
        calendar_id=RECLAIM_CAL,
        summary="Focus Time",
        start="2026-07-01T11:30:00-05:00",
        end="2026-07-01T12:00:00-05:00",
        is_reclaim_travel=False,
    )
    ops = plan_reconciliation(_two_leg_trip(same_airport=True), [focus_block], _config())
    assert _ops_of(ops, kind=KIND_RECLAIM_TRAVEL) == []


def test_reclaim_kept_outside_the_gap():
    # Reclaim block before the first leg (home -> airport) is never in a gap.
    before = _event(
        event_id="r1",
        calendar_id=RECLAIM_CAL,
        start="2026-07-01T06:30:00-05:00",
        end="2026-07-01T07:30:00-05:00",
    )
    ops = plan_reconciliation(_two_leg_trip(same_airport=True), [before], _config())
    assert _ops_of(ops, kind=KIND_RECLAIM_TRAVEL) == []


# --- teardown ----------------------------------------------------------


def _full_ledger():
    return {
        KIND_BOARDING: {
            "event_id": "b1",
            "calendar_id": BYAIR_CAL,
            "managed": MANAGED_CREATED,
            "synced_signature": "x",
        },
        KIND_FLIGHT: {
            "event_id": "f1",
            "calendar_id": BYAIR_CAL,
            "managed": MANAGED_ADOPTED,
            "synced_signature": "y",
        },
    }


def test_teardown_deletes_managed_events_on_cancel():
    flight = _flight(disposition="cancelled", calendar_events=_full_ledger())
    ops = plan_reconciliation([flight], [], _config())
    assert {o["event_id"] for o in _ops_of(ops, op="delete")} == {"b1", "f1"}
    assert {o["kind"] for o in _ops_of(ops, op="delete")} == {KIND_BOARDING, KIND_FLIGHT}


def test_teardown_on_switched_away():
    flight = _flight(disposition="switched_away", calendar_events=_full_ledger())
    ops = plan_reconciliation([flight], [], _config())
    assert len(_ops_of(ops, op="delete")) == 2


def test_completed_flight_left_untouched():
    flight = _flight(disposition="completed", calendar_events=_full_ledger())
    assert plan_reconciliation([flight], [], _config()) == []


def test_cancelled_flight_gets_no_boarding_or_adopt():
    # A torn-down flight must not also be reconciled as active.
    flight = _flight(disposition="cancelled", calendar_events=_full_ledger())
    byair_event = _event(event_id="f9", summary="AA100")
    ops = plan_reconciliation([flight], [byair_event], _config())
    assert _ops_of(ops, op="create") == []
    assert _ops_of(ops, op="adopt") == []
