"""notices.py: one fixed template per event reason, merged per flight (#370).

Event shapes follow references/event-payloads.md; the expected wording is the
SKILL table the model used before, and the live 2026-11-02 boarding notice
("Boarding now: DL…. Gate B….").
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[1] / "skills/flight-assist"
sys.path.insert(0, str(BUNDLE))

# Insert the shipped bundle path before importing its module.
from notices import ORDER, SILENT, SOURCE_REASONS, compose, line  # noqa: E402

STATE = {
    "code": "DL1234",
    "scheduled_dep_time": "2026-11-02T10:05:00-06:00",
    "scheduled_arr_time": "2026-11-02T13:40:00-05:00",
    "last_snapshot": {
        "dep_gate": "B12",
        "dep_terminal": None,
        "dep_airport_code": "BNA",
        "arr_airport_code": "LGA",
        "arr_airport_name": "LaGuardia",
        "arr_time": "2026-11-02T13:52:00-05:00",
        "arr_terminal": "C",
        "arr_gate": "C40",
        "baggage": "5",
    },
}


@pytest.mark.parametrize(
    "event, text",
    [
        ({"reason": "cancelled"}, "Flight DL1234 cancelled."),
        ({"reason": "diverted"}, "Flight DL1234 diverted."),
        (
            {"reason": "gate_assignment", "dep_gate": "E16", "dep_terminal": "2"},
            "DL1234 departs Terminal 2, Gate E16.",
        ),
        (
            {"reason": "gate_assignment", "dep_gate": "E16", "dep_terminal": None},
            "DL1234 departs Gate E16.",
        ),
        (
            {"reason": "gate_change", "side": "dep", "from": "B25", "to": "B7"},
            "Gate change: DL1234 moved from B25 to B7.",
        ),
        (
            {"reason": "gate_change", "side": "dep", "from": "B25", "to": None},
            "Gate B25 removed from DL1234.",
        ),
        (
            {"reason": "gate_change", "side": "arr", "from": "C1", "to": "C9"},
            "Arrival gate change: DL1234 moved from C1 to C9.",
        ),
        (
            {"reason": "delay", "delay_minutes": 22, "new_dep_time": "2026-11-02T10:27:00-06:00"},
            "Flight DL1234 delayed by 22 min. New departure: 10:27 (local).",
        ),
        (
            {"reason": "delay", "delay_minutes": -10, "new_dep_time": "2026-11-02T09:55:00-06:00"},
            "Flight DL1234 moved earlier by 10 min. New departure: 09:55 (local).",
        ),
        (
            {
                "reason": "inbound_delay_predicted",
                "delay_minutes": 35,
                "predicted_time": "2026-11-02T10:40:00-06:00",
            },
            "Inbound aircraft delay predicted: 35 min for DL1234. New estimated departure: 10:40.",
        ),
        (
            {
                "reason": "inbound_delay_retracted",
                "prev_delay_minutes": 95,
                "new_delay_minutes": None,
            },
            "Inbound delay for DL1234 has cleared — previously predicted 95 min, no longer "
            "predicted. DL1234 is back on track.",
        ),
        ({"reason": "boarding_started"}, "Boarding now: DL1234. Gate B12."),
        ({"reason": "carousel_revealed", "baggage": "CLM1"}, "Baggage carousel for DL1234: CLM1."),
        (
            {
                "reason": "time_to_leave",
                "leave_by": "2026-11-02T14:15:00+00:00",
                "travel_time_minutes": 30,
                "scheduled_dep_time": "2026-11-02T10:05:00-06:00",
                "missed": False,
            },
            "Leave by 08:15 to make DL1234 (30 min drive with current traffic).",
        ),
        (
            {"reason": "arrival_logistics", "scheduled_arr_time": "2026-11-02T13:40:00-05:00"},
            "DL1234 lands at LaGuardia around 13:52. Terminal C, Gate C40. Bags: carousel 5.",
        ),
        (
            {"reason": "removed_upstream"},
            "Flight DL1234 no longer tracked upstream — remove from active trips if intentional.",
        ),
    ],
)
def test_each_reason_renders_its_template(event, text):
    assert line(event, STATE) == text


def test_a_missed_leave_by_says_so():
    event = {
        "reason": "time_to_leave",
        "leave_by": "2026-11-02T14:45:00+00:00",
        "travel_time_minutes": 0,
        "scheduled_dep_time": "2026-11-02T10:05:00-06:00",
        "missed": True,
    }
    assert (line(event, STATE) or "").startswith("Leave-by 08:45 for DL1234 has passed")


def test_connection_risk_names_both_legs():
    tight = {
        "reason": "connection_at_risk",
        "leg1_code": "DL1",
        "leg2_code": "DL2",
        "transfer_minutes_remaining": 32,
        "min_transfer_minutes": 45,
        "missed_connection": False,
    }
    assert line(tight, STATE) == (
        "Tight connection: DL2 boards in 32 min after DL1 arrives — below the 45-min buffer. "
        "Consider rebooking now."
    )
    missed = {**tight, "missed_connection": True}
    assert (line(missed, STATE) or "").startswith("Connection structurally missed: DL1")


def test_day_before_lists_the_calendar_and_closing_windows():
    event = {
        "reason": "day_before",
        "scheduled_dep_time": STATE["scheduled_dep_time"],
        "day_label": "tomorrow",
    }
    calendar = {
        "events": [
            {"summary": "Design review", "display": "Tue 15:00–16:00", "window": False},
            {"summary": "Sound check", "end": "2026-11-02T17:00:00-05:00", "window": True},
        ]
    }
    assert line(event, STATE, calendar) == (
        "Tomorrow: DL1234 BNA → LGA, departs 10:05.\n"
        "Near it on your calendar:\n"
        "• Design review Tue 15:00–16:00\n"
        "• Sound check: drop-in window you can't reach before it closes at 17:00"
    )


def test_day_before_with_a_date_label_and_no_calendar():
    event = {
        "reason": "day_before",
        "scheduled_dep_time": STATE["scheduled_dep_time"],
        "day_label": "2026-11-02",
    }
    assert line(event, STATE, None) == "Mon 11/2: DL1234 BNA → LGA, departs 10:05."


@pytest.mark.parametrize(
    "event, start",
    [
        (
            {"reason": "byair_unreachable", "consecutive_failures": 3, "error": "session_missing"},
            "Flight sync with byAir is failing: 3 passes",
        ),
        ({"reason": "byair_recovered"}, "Flight sync with byAir recovered."),
        (
            {"reason": "sync_internal_error", "consecutive_failures": 3, "error": "ValueError"},
            "Flight sync is failing inside NanoClaw: 3",
        ),
        ({"reason": "sync_internal_recovered"}, "The local fault in flight sync has cleared."),
        (
            {
                "reason": "tracked_flight_removed",
                "code": "DL9",
                "scheduled_dep_time": "2026-12-01T08:00:00Z",
            },
            "Flight DL9 on 2026-12-01 disappeared from tracking",
        ),
    ],
)
def test_sync_reasons_render_without_flight_state(event, start):
    assert (line(event, None) or "").startswith(start)


def test_every_known_reason_has_a_template_and_unknown_ones_fail():
    for reason in set(ORDER) | SOURCE_REASONS:
        assert line({"reason": reason}, STATE) is not None, reason
    for reason in SILENT:
        assert line({"reason": reason}, STATE) is None
    with pytest.raises(ValueError, match="no notice template"):
        line({"reason": "teleported"}, STATE)


def test_one_flight_merges_in_priority_order_and_sources_stay_separate():
    items = [
        {
            "flight_id": 7,
            "event": {"reason": "carousel_revealed", "baggage": "5"},
            "notification_id": "c",
        },
        {"flight_id": 7, "event": {"reason": "cancelled"}, "notification_id": "a"},
        {
            "flight_id": 7,
            "event": {"reason": "gate_change", "side": "dep", "from": "B1", "to": "B2"},
            "notification_id": "b",
        },
        {"flight_id": None, "event": {"reason": "byair_recovered"}},
        {"flight_id": None, "event": {"reason": "sync_internal_recovered"}},
    ]
    messages = compose(items, lambda fid: STATE, lambda fid: None)
    assert messages == [
        ([], "Flight sync with byAir recovered."),
        ([], "The local fault in flight sync has cleared."),
        (
            ["a", "b", "c"],
            "Flight DL1234 cancelled.\nGate change: DL1234 moved from B1 to B2.\n"
            "Baggage carousel for DL1234: 5.",
        ),
    ]


def test_the_calendar_is_read_only_for_a_day_before_flight():
    asked = []

    def calendar(fid):
        asked.append(fid)
        return None

    items = [
        {"flight_id": 7, "event": {"reason": "cancelled"}, "notification_id": "a"},
        {
            "flight_id": 8,
            "event": {"reason": "day_before", "day_label": "today"},
            "notification_id": "b",
        },
    ]
    compose(items, lambda fid: STATE, calendar)
    assert asked == [8]


def test_new_flights_are_announced_only_when_nothing_else_came():
    added = {
        "flight_id": 9,
        "event": {
            "reason": "tracked_flight_added",
            "code": "DL9",
            "scheduled_dep_time": "2026-12-01T08:00:00Z",
        },
    }
    assert compose([added], lambda fid: None, lambda fid: None) == [
        ([], "Now tracking DL9 on 2026-12-01.")
    ]
    other = {"flight_id": None, "event": {"reason": "byair_recovered"}}
    assert compose([added, other], lambda fid: None, lambda fid: None) == [
        ([], "Flight sync with byAir recovered.")
    ]


def test_day_before_without_a_usable_label_leaves_the_day_out():
    for label in (None, "soon", 7):
        event = {
            "reason": "day_before",
            "scheduled_dep_time": STATE["scheduled_dep_time"],
            "day_label": label,
        }
        assert line(event, STATE, None) == "DL1234 BNA → LGA, departs 10:05."


def test_a_busy_calendar_stays_under_the_send_limit():
    event = {
        "reason": "day_before",
        "scheduled_dep_time": STATE["scheduled_dep_time"],
        "day_label": "tomorrow",
    }
    calendar = {"events": [{"summary": "M" * 500, "display": "Tue 15:00", "window": False}] * 40}
    text = line(event, STATE, calendar) or ""
    assert len(text) < 4000
    assert text.count("\n• ") == 13 and text.endswith("• …and 28 more")
