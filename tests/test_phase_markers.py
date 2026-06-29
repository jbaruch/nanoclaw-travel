"""Tests for skills/flight-assist/phase_markers.py.

Time-based wake-gate logic with synthetic `now_utc` to keep tests
deterministic — no `datetime.now()` calls inside the module under
test reach a real clock.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from phase_markers import (  # noqa: E402
    ARRIVAL_LOGISTICS_LEAD_MINUTES,
    DAY_BEFORE_HOURS,
    TIME_TO_LEAVE_BUFFER_MINUTES,
    check_arrival_logistics,
    check_day_before,
    check_gate_assignment,
    check_time_to_leave,
)

SCHED_DEP = "2026-05-18T17:00:00+00:00"
SCHED_ARR = "2026-05-18T20:00:00+00:00"


def _markers(**overrides) -> dict:
    base = {
        "day_before_fired": False,
        "time_to_leave_fired": False,
        "boarding_fired": False,
        "arrival_logistics_fired": False,
        "landed_acknowledged": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# day_before
# ---------------------------------------------------------------------------


def test_day_before_does_not_fire_before_threshold():
    now = datetime(2026, 5, 17, 16, 59, 0, tzinfo=timezone.utc)  # T-24h01min
    fired, _ = check_day_before(scheduled_dep_time=SCHED_DEP, phase_markers=_markers(), now_utc=now)
    assert fired is False


def test_day_before_fires_at_exact_threshold():
    """Exactly T-24h ago is the firing point."""
    now = datetime(2026, 5, 17, 17, 0, 0, tzinfo=timezone.utc)  # T-24h
    fired, event = check_day_before(
        scheduled_dep_time=SCHED_DEP, phase_markers=_markers(), now_utc=now
    )
    assert fired is True
    assert event["reason"] == "day_before"
    assert event["hours_until_dep"] == DAY_BEFORE_HOURS


def test_day_before_fires_after_threshold():
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)  # T-7h
    fired, _ = check_day_before(scheduled_dep_time=SCHED_DEP, phase_markers=_markers(), now_utc=now)
    assert fired is True


def test_day_before_does_not_re_fire_when_marker_set():
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    fired, _ = check_day_before(
        scheduled_dep_time=SCHED_DEP,
        phase_markers=_markers(day_before_fired=True),
        now_utc=now,
    )
    assert fired is False


def test_day_before_with_malformed_time_does_not_fire():
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)
    fired, _ = check_day_before(
        scheduled_dep_time="not-a-time", phase_markers=_markers(), now_utc=now
    )
    assert fired is False


# ---------------------------------------------------------------------------
# time_to_leave
# ---------------------------------------------------------------------------


def test_time_to_leave_does_not_fire_when_travel_time_none():
    """No traffic estimate yet — defer the decision."""
    now = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)
    fired, _ = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=None,
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is False


def test_time_to_leave_does_not_fire_before_leave_by():
    """now + travel + buffer < scheduled_dep — still time."""
    travel_minutes = 30
    # Dep is 17:00; with 30 min drive + 15 min buffer, leave_by = 16:15.
    # At 14:00 we have 2h15m before leave_by — way too early.
    now = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)
    fired, _ = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=travel_minutes * 60,
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is False


def test_time_to_leave_fires_at_leave_by():
    """At exactly leave_by, fire."""
    travel_minutes = 30
    # leave_by = 17:00 - 30min - 15min = 16:15
    now = datetime(2026, 5, 18, 16, 15, 0, tzinfo=timezone.utc)
    fired, event = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=travel_minutes * 60,
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is True
    assert event["reason"] == "time_to_leave"
    assert event["travel_time_minutes"] == travel_minutes


def test_time_to_leave_fires_past_leave_by():
    """After leave_by (user is already late) — still fire."""
    travel_minutes = 30
    now = datetime(2026, 5, 18, 16, 45, 0, tzinfo=timezone.utc)  # 30 min past leave_by
    fired, _ = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=travel_minutes * 60,
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is True


def test_time_to_leave_does_not_re_fire_when_marker_set():
    now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
    fired, _ = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=1800,
        phase_markers=_markers(time_to_leave_fired=True),
        now_utc=now,
    )
    assert fired is False


def test_time_to_leave_buffer_applied():
    """leave_by accounts for TIME_TO_LEAVE_BUFFER_MINUTES on top of travel."""
    # travel = 0, so leave_by = dep - buffer
    now = datetime(2026, 5, 18, 16, 45, 0, tzinfo=timezone.utc)
    # 16:45 is dep - 15min, which is exactly leave_by when travel=0
    fired, _ = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=0,
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is True
    # One minute earlier — should not fire
    earlier = now - timedelta(minutes=1)
    fired_earlier, _ = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=0,
        phase_markers=_markers(),
        now_utc=earlier,
    )
    assert fired_earlier is False
    assert TIME_TO_LEAVE_BUFFER_MINUTES == 15  # documenting the constant


def _boarding_snapshot(**overrides) -> dict:
    """A snapshot byAir reports as genuinely boarding (not the premature label)."""
    base = {
        "computed_status": "boarding",
        "computed_status_detail": "Boarding now",
        "dep_gate": "B7",
    }
    base.update(overrides)
    return base


def test_time_to_leave_suppressed_when_really_boarding():
    """#102 — leave-by is moot once boarding has actually started, even if the
    leave-by threshold is met (delayed flight / stale travel estimate)."""
    now = datetime(2026, 5, 18, 16, 45, 0, tzinfo=timezone.utc)  # past leave_by
    fired, _ = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=1800,
        phase_markers=_markers(),
        now_utc=now,
        snapshot=_boarding_snapshot(),
    )
    assert fired is False


def test_time_to_leave_not_suppressed_by_premature_boarding_label():
    """byAir's early "boarding" label (detail still counting down) is not real
    boarding — the leave-by alert must still fire."""
    now = datetime(2026, 5, 18, 16, 45, 0, tzinfo=timezone.utc)
    fired, _ = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=1800,
        phase_markers=_markers(),
        now_utc=now,
        snapshot=_boarding_snapshot(computed_status_detail="Boarding starts in 35 min"),
    )
    assert fired is True


def test_time_to_leave_suppressed_when_departed_or_cancelled():
    """Once the flight has left or been cancelled, leave-by never fires."""
    now = datetime(2026, 5, 18, 16, 45, 0, tzinfo=timezone.utc)
    for status in ("departed", "en_route", "landed", "cancelled", "diverted"):
        fired, _ = check_time_to_leave(
            scheduled_dep_time=SCHED_DEP,
            travel_time_seconds=1800,
            phase_markers=_markers(),
            now_utc=now,
            snapshot={"computed_status": status},
        )
        assert fired is False, f"expected suppression for status {status!r}"


def test_time_to_leave_fires_pre_boarding_with_scheduled_snapshot():
    """Regression: a still-scheduled flight at the leave-by threshold fires."""
    now = datetime(2026, 5, 18, 16, 45, 0, tzinfo=timezone.utc)
    fired, event = check_time_to_leave(
        scheduled_dep_time=SCHED_DEP,
        travel_time_seconds=0,
        phase_markers=_markers(),
        now_utc=now,
        snapshot={"computed_status": "scheduled", "dep_gate": None},
    )
    assert fired is True
    assert event["reason"] == "time_to_leave"


# ---------------------------------------------------------------------------
# arrival_logistics
# ---------------------------------------------------------------------------


def test_arrival_logistics_does_not_fire_before_threshold():
    now = datetime(2026, 5, 18, 19, 44, 0, tzinfo=timezone.utc)  # T-arr - 16min
    fired, _ = check_arrival_logistics(
        scheduled_arr_time=SCHED_ARR, phase_markers=_markers(), now_utc=now
    )
    assert fired is False


def test_arrival_logistics_fires_at_threshold():
    now = datetime(2026, 5, 18, 19, 45, 0, tzinfo=timezone.utc)  # T-arr - 15min
    fired, event = check_arrival_logistics(
        scheduled_arr_time=SCHED_ARR, phase_markers=_markers(), now_utc=now
    )
    assert fired is True
    assert event["reason"] == "arrival_logistics"
    assert event["minutes_until_arr"] == ARRIVAL_LOGISTICS_LEAD_MINUTES


def test_arrival_logistics_fires_after_threshold():
    now = datetime(2026, 5, 18, 19, 55, 0, tzinfo=timezone.utc)  # T-arr - 5min
    fired, _ = check_arrival_logistics(
        scheduled_arr_time=SCHED_ARR, phase_markers=_markers(), now_utc=now
    )
    assert fired is True


def test_arrival_logistics_does_not_re_fire_when_marker_set():
    now = datetime(2026, 5, 18, 19, 50, 0, tzinfo=timezone.utc)
    fired, _ = check_arrival_logistics(
        scheduled_arr_time=SCHED_ARR,
        phase_markers=_markers(arrival_logistics_fired=True),
        now_utc=now,
    )
    assert fired is False


def test_arrival_logistics_with_malformed_time_does_not_fire():
    now = datetime(2026, 5, 18, 20, 0, 0, tzinfo=timezone.utc)
    fired, _ = check_arrival_logistics(
        scheduled_arr_time=None, phase_markers=_markers(), now_utc=now
    )
    assert fired is False


# ---------------------------------------------------------------------------
# gate_assignment readout (#103)
# ---------------------------------------------------------------------------

# Narrowbody lead = 30 min, so the window opens at 17:00 − 30 − 60 = 15:30.
NARROWBODY_LEAD = 30
WIDEBODY_LEAD = 50


def _gate_snapshot(*, dep_gate: str | None, dep_terminal: str | None = None) -> dict:
    return {"computed_status": "scheduled", "dep_gate": dep_gate, "dep_terminal": dep_terminal}


def test_gate_assignment_does_not_fire_before_window():
    """Gate present at T−3h (before the window) — no readout."""
    now = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)  # T−3h, window opens 15:30
    fired, _ = check_gate_assignment(
        scheduled_dep_time=SCHED_DEP,
        boarding_lead_minutes=NARROWBODY_LEAD,
        snapshot=_gate_snapshot(dep_gate="E16", dep_terminal="2"),
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is False


def test_gate_assignment_fires_when_window_opens_with_gate():
    """Window opens with a gate present → one readout carrying gate + terminal."""
    now = datetime(2026, 5, 18, 15, 30, 0, tzinfo=timezone.utc)  # exactly window-open
    fired, event = check_gate_assignment(
        scheduled_dep_time=SCHED_DEP,
        boarding_lead_minutes=NARROWBODY_LEAD,
        snapshot=_gate_snapshot(dep_gate="E16", dep_terminal="2"),
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is True
    assert event["reason"] == "gate_assignment"
    assert event["dep_gate"] == "E16"
    assert event["dep_terminal"] == "2"


def test_gate_assignment_defers_until_gate_appears_in_window():
    """Gate null when the window opens → defer; readout fires on first appearance."""
    in_window = datetime(2026, 5, 18, 15, 40, 0, tzinfo=timezone.utc)
    fired_no_gate, _ = check_gate_assignment(
        scheduled_dep_time=SCHED_DEP,
        boarding_lead_minutes=NARROWBODY_LEAD,
        snapshot=_gate_snapshot(dep_gate=None),
        phase_markers=_markers(),
        now_utc=in_window,
    )
    assert fired_no_gate is False

    later = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    fired_gate, event = check_gate_assignment(
        scheduled_dep_time=SCHED_DEP,
        boarding_lead_minutes=NARROWBODY_LEAD,
        snapshot=_gate_snapshot(dep_gate="C2"),
        phase_markers=_markers(),
        now_utc=later,
    )
    assert fired_gate is True
    assert event["dep_gate"] == "C2"
    assert event["dep_terminal"] is None  # no terminal published yet


def test_gate_assignment_does_not_re_fire_when_marker_set():
    now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    fired, _ = check_gate_assignment(
        scheduled_dep_time=SCHED_DEP,
        boarding_lead_minutes=NARROWBODY_LEAD,
        snapshot=_gate_snapshot(dep_gate="E16", dep_terminal="2"),
        phase_markers=_markers(gate_assignment_fired=True),
        now_utc=now,
    )
    assert fired is False


def test_gate_assignment_window_widens_for_widebody_lead():
    """A 50-min boarding lead opens the window 20 min earlier than the 30-min lead."""
    # At 15:20: narrowbody window (opens 15:30) not yet open; widebody (opens 15:10) is.
    now = datetime(2026, 5, 18, 15, 20, 0, tzinfo=timezone.utc)
    snapshot = _gate_snapshot(dep_gate="E16", dep_terminal="2")
    fired_narrow, _ = check_gate_assignment(
        scheduled_dep_time=SCHED_DEP,
        boarding_lead_minutes=NARROWBODY_LEAD,
        snapshot=snapshot,
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired_narrow is False
    fired_wide, _ = check_gate_assignment(
        scheduled_dep_time=SCHED_DEP,
        boarding_lead_minutes=WIDEBODY_LEAD,
        snapshot=snapshot,
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired_wide is True


def test_gate_assignment_suppressed_when_boarding_or_gone():
    """A flight already boarding/departed/cancelled/diverted gets no readout —
    navigating to a departure gate is moot by then."""
    now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)  # in window
    for snapshot in (
        {
            "computed_status": "boarding",
            "computed_status_detail": "Boarding now",
            "dep_gate": "E16",
        },
        {"computed_status": "departed", "dep_gate": "E16"},
        {"computed_status": "cancelled", "dep_gate": "E16"},
        {"computed_status": "diverted", "dep_gate": "E16"},
    ):
        fired, _ = check_gate_assignment(
            scheduled_dep_time=SCHED_DEP,
            boarding_lead_minutes=NARROWBODY_LEAD,
            snapshot=snapshot,
            phase_markers=_markers(),
            now_utc=now,
        )
        assert fired is False, f"expected suppression for {snapshot['computed_status']!r}"


def test_gate_assignment_not_suppressed_by_premature_boarding_label():
    """byAir's early "boarding" label (detail still counting down) is not real
    boarding — the readout must still fire."""
    now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    fired, event = check_gate_assignment(
        scheduled_dep_time=SCHED_DEP,
        boarding_lead_minutes=NARROWBODY_LEAD,
        snapshot={
            "computed_status": "boarding",
            "computed_status_detail": "Boarding starts in 25 min",
            "dep_gate": "E16",
            "dep_terminal": "2",
        },
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is True
    assert event["dep_gate"] == "E16"


def test_gate_assignment_with_malformed_time_does_not_fire():
    now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    fired, _ = check_gate_assignment(
        scheduled_dep_time="not-a-time",
        boarding_lead_minutes=NARROWBODY_LEAD,
        snapshot=_gate_snapshot(dep_gate="E16"),
        phase_markers=_markers(),
        now_utc=now,
    )
    assert fired is False


# ---------------------------------------------------------------------------
# Naive-datetime handling
# ---------------------------------------------------------------------------


def test_naive_iso_string_treated_as_utc():
    """A scheduled time without offset is treated as UTC (per parse helper docstring)."""
    sched = "2026-05-18T17:00:00"  # no offset
    now = datetime(2026, 5, 18, 10, 0, 0, tzinfo=timezone.utc)  # T-7h
    fired, _ = check_day_before(scheduled_dep_time=sched, phase_markers=_markers(), now_utc=now)
    assert fired is True
