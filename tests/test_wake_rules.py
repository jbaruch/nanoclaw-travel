"""Tests for skills/flight-assist/wake_rules.py.

Pure-function tests with synthetic snapshot fixtures. No fixtures
shared across tests; each test constructs its own minimal pair per
`coding-policy: testing-standards` "Independence".
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from wake_rules import (  # noqa: E402
    DELAY_THRESHOLD_MINUTES,
    INBOUND_DELAY_DEDUPE_MINUTES,
    INBOUND_DELAY_THRESHOLD_MINUTES,
    detect_wake_events,
)


def _snapshot(**overrides) -> dict:
    """Build a synthetic flight snapshot matching the trimmed last_snapshot shape."""
    base = {
        "code": "XX123",
        "computed_status": "scheduled",
        "computed_status_detail": "Departing in 3h",
        "computed_phase_progress": None,
        "computed_phase_risk": None,
        "computed_phase_overdue": None,
        "dep_gate": None,
        "arr_gate": None,
        "dep_terminal": None,
        "arr_terminal": None,
        "dep_time": "2026-05-17T09:00:00-07:00",
        "arr_time": "2026-05-17T11:09:00-07:00",
        "baggage": None,
        "inbound": None,
        "position_lat": None,
        "position_lon": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# First-cycle behavior (prev is None)
# ---------------------------------------------------------------------------


def test_first_cycle_scheduled_flight_fires_no_events():
    events = detect_wake_events(prev=None, new=_snapshot())
    assert events == []


def test_first_cycle_already_cancelled_fires_cancelled():
    events = detect_wake_events(prev=None, new=_snapshot(computed_status="cancelled"))
    reasons = [e["reason"] for e in events]
    assert "cancelled" in reasons


def test_first_cycle_already_diverted_fires_diverted():
    events = detect_wake_events(prev=None, new=_snapshot(computed_status="diverted"))
    reasons = [e["reason"] for e in events]
    assert "diverted" in reasons


def test_first_cycle_already_boarding_does_not_fire_boarding_started():
    """No prior snapshot to confirm a transition — phase_markers handles this case."""
    events = detect_wake_events(prev=None, new=_snapshot(computed_status="boarding"))
    reasons = [e["reason"] for e in events]
    assert "boarding_started" not in reasons


def test_first_cycle_gate_set_does_not_fire_gate_change():
    """First sight of a gate is the schedule revealing info, not a re-gate."""
    events = detect_wake_events(prev=None, new=_snapshot(dep_gate="B25"))
    assert events == []


# ---------------------------------------------------------------------------
# Status transitions (cancelled, diverted, boarding)
# ---------------------------------------------------------------------------


def test_transition_to_cancelled_fires_cancelled():
    prev = _snapshot(computed_status="scheduled")
    new = _snapshot(computed_status="cancelled")
    events = detect_wake_events(prev, new)
    assert {"reason": "cancelled"} in events


def test_already_cancelled_to_cancelled_does_not_re_fire():
    prev = _snapshot(computed_status="cancelled")
    new = _snapshot(computed_status="cancelled")
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "cancelled" for e in events)


def test_transition_to_diverted_fires_diverted():
    prev = _snapshot(computed_status="en_route")
    new = _snapshot(computed_status="diverted")
    events = detect_wake_events(prev, new)
    assert {"reason": "diverted"} in events


def test_transition_scheduled_to_boarding_fires_boarding_started():
    prev = _snapshot(computed_status="check_in_open")
    new = _snapshot(computed_status="boarding")
    events = detect_wake_events(prev, new)
    assert {"reason": "boarding_started"} in events


def test_boarding_to_boarding_does_not_re_fire():
    prev = _snapshot(computed_status="boarding")
    new = _snapshot(computed_status="boarding")
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "boarding_started" for e in events)


# ---------------------------------------------------------------------------
# Premature "boarding" label (#54): byAir flips computed_status to
# "boarding" up to ~1h before boarding starts, while computed_status_detail
# still says "Boarding starts in N min" and computed_phase_progress is 0.
# The label alone must NOT fire a boarding alert.
# ---------------------------------------------------------------------------


def test_premature_boarding_label_does_not_fire_boarding_started():
    """status=boarding but detail says boarding is still in the future → suppress."""
    prev = _snapshot(computed_status="check_in_open")
    new = _snapshot(
        computed_status="boarding",
        computed_status_detail="Boarding starts in 1h 19min",
        computed_phase_progress=0,
    )
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "boarding_started" for e in events)


def test_real_boarding_fires_even_after_premature_label():
    """A flight byAir already marked 'boarding' prematurely must still fire
    once the detail flips to actual boarding — the raw computed_status never
    changes across that flip, so the gate keys off the real-boarding signal."""
    prev = _snapshot(
        computed_status="boarding",
        computed_status_detail="Boarding starts in 1h 19min",
        computed_phase_progress=0,
    )
    new = _snapshot(
        computed_status="boarding",
        computed_status_detail="Boarding now — gate B7",
        computed_phase_progress=10,
    )
    events = detect_wake_events(prev, new)
    assert {"reason": "boarding_started"} in events


def test_real_boarding_detail_fires_boarding_started():
    """status=boarding with a non-future detail is genuine boarding → fire."""
    prev = _snapshot(computed_status="check_in_open")
    new = _snapshot(
        computed_status="boarding",
        computed_status_detail="Boarding now",
        computed_phase_progress=5,
    )
    events = detect_wake_events(prev, new)
    assert {"reason": "boarding_started"} in events


def test_first_cycle_premature_boarding_does_not_fire():
    """No prior snapshot AND a premature label — still no boarding alert."""
    new = _snapshot(
        computed_status="boarding",
        computed_status_detail="Boarding starts in 50min",
        computed_phase_progress=0,
    )
    events = detect_wake_events(prev=None, new=new)
    assert not any(e["reason"] == "boarding_started" for e in events)


# ---------------------------------------------------------------------------
# Gate change
# ---------------------------------------------------------------------------


def test_dep_gate_change_fires_gate_change():
    prev = _snapshot(dep_gate="B25")
    new = _snapshot(dep_gate="B7")
    events = detect_wake_events(prev, new)
    assert {"reason": "gate_change", "side": "dep", "from": "B25", "to": "B7"} in events


def test_arr_gate_change_fires_gate_change():
    prev = _snapshot(arr_gate="A26")
    new = _snapshot(arr_gate="A3")
    events = detect_wake_events(prev, new)
    assert {"reason": "gate_change", "side": "arr", "from": "A26", "to": "A3"} in events


def test_first_gate_assignment_does_not_fire():
    """None → 'B25' is not a re-gate; it's first publication of the gate."""
    prev = _snapshot(dep_gate=None)
    new = _snapshot(dep_gate="B25")
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "gate_change" for e in events)


def test_gate_unchanged_does_not_fire():
    prev = _snapshot(dep_gate="B25")
    new = _snapshot(dep_gate="B25")
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "gate_change" for e in events)


def test_gate_removal_fires_gate_change():
    """B25 → None is a change worth surfacing — the data feed lost info."""
    prev = _snapshot(dep_gate="B25")
    new = _snapshot(dep_gate=None)
    events = detect_wake_events(prev, new)
    assert {"reason": "gate_change", "side": "dep", "from": "B25", "to": None} in events


# ---------------------------------------------------------------------------
# Delay (dep_time shift)
# ---------------------------------------------------------------------------


def test_delay_below_threshold_does_not_fire():
    prev = _snapshot(dep_time="2026-05-17T09:00:00-07:00")
    new = _snapshot(dep_time="2026-05-17T09:14:00-07:00")  # 14 min
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "delay" for e in events)


def test_delay_at_threshold_fires():
    prev = _snapshot(dep_time="2026-05-17T09:00:00-07:00")
    new = _snapshot(dep_time="2026-05-17T09:15:00-07:00")  # exactly 15 min
    events = detect_wake_events(prev, new)
    delay_events = [e for e in events if e["reason"] == "delay"]
    assert len(delay_events) == 1
    assert delay_events[0]["delay_minutes"] == DELAY_THRESHOLD_MINUTES


def test_delay_well_above_threshold_fires():
    prev = _snapshot(dep_time="2026-05-17T09:00:00-07:00")
    new = _snapshot(dep_time="2026-05-17T10:30:00-07:00")  # 90 min
    events = detect_wake_events(prev, new)
    delay_events = [e for e in events if e["reason"] == "delay"]
    assert len(delay_events) == 1
    assert delay_events[0]["delay_minutes"] == 90


def test_delay_with_advanced_time_fires_negative_delay():
    """A flight moved EARLIER by ≥ threshold is also news-worthy."""
    prev = _snapshot(dep_time="2026-05-17T09:00:00-07:00")
    new = _snapshot(dep_time="2026-05-17T08:30:00-07:00")  # -30 min
    events = detect_wake_events(prev, new)
    delay_events = [e for e in events if e["reason"] == "delay"]
    assert len(delay_events) == 1
    assert delay_events[0]["delay_minutes"] == -30


def test_delay_with_missing_prev_dep_time_does_not_fire():
    prev = _snapshot(dep_time=None)
    new = _snapshot(dep_time="2026-05-17T09:30:00-07:00")
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "delay" for e in events)


def test_delay_across_dst_offset_handled_via_utc():
    """Even if timezones shift, comparison via UTC normalizes correctly."""
    prev = _snapshot(dep_time="2026-05-17T09:00:00-07:00")  # 16:00 UTC
    new = _snapshot(dep_time="2026-05-17T17:30:00-06:00")  # 23:30 UTC, +7h30m
    events = detect_wake_events(prev, new)
    delay_events = [e for e in events if e["reason"] == "delay"]
    assert len(delay_events) == 1
    assert delay_events[0]["delay_minutes"] == 450  # 7h30m


# ---------------------------------------------------------------------------
# Inbound delay prediction
# ---------------------------------------------------------------------------


def test_inbound_delay_below_threshold_does_not_fire():
    new = _snapshot(inbound={"predicted_delay_minutes": INBOUND_DELAY_THRESHOLD_MINUTES - 1})
    events = detect_wake_events(prev=None, new=new)
    assert not any(e["reason"] == "inbound_delay_predicted" for e in events)


def test_inbound_delay_at_threshold_fires():
    new = _snapshot(
        inbound={
            "predicted_delay_minutes": INBOUND_DELAY_THRESHOLD_MINUTES,
            "predicted_time": "2026-05-17T09:25:00-07:00",
        }
    )
    events = detect_wake_events(prev=None, new=new)
    inbound_events = [e for e in events if e["reason"] == "inbound_delay_predicted"]
    assert len(inbound_events) == 1
    assert inbound_events[0]["delay_minutes"] == INBOUND_DELAY_THRESHOLD_MINUTES
    assert inbound_events[0]["predicted_time"] == "2026-05-17T09:25:00-07:00"


def test_inbound_delay_already_fired_at_similar_magnitude_does_not_re_fire():
    prev = _snapshot(inbound={"predicted_delay_minutes": 30})
    new = _snapshot(inbound={"predicted_delay_minutes": 32})  # within dedupe window
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "inbound_delay_predicted" for e in events)


def test_inbound_delay_increased_beyond_dedupe_re_fires():
    prev = _snapshot(inbound={"predicted_delay_minutes": 30})
    new = _snapshot(
        inbound={
            "predicted_delay_minutes": 30 + INBOUND_DELAY_DEDUPE_MINUTES + 1,
            "predicted_time": "...",
        }
    )
    events = detect_wake_events(prev, new)
    inbound_events = [e for e in events if e["reason"] == "inbound_delay_predicted"]
    assert len(inbound_events) == 1


def test_inbound_with_no_predicted_delay_does_not_fire():
    new = _snapshot(inbound={"aircraft_model": "A320", "registration": "N123"})
    events = detect_wake_events(prev=None, new=new)
    assert not any(e["reason"] == "inbound_delay_predicted" for e in events)


def test_inbound_with_zero_delay_does_not_fire():
    new = _snapshot(inbound={"predicted_delay_minutes": 0})
    events = detect_wake_events(prev=None, new=new)
    assert not any(e["reason"] == "inbound_delay_predicted" for e in events)


def test_inbound_with_negative_delay_does_not_fire():
    """Negative = inbound aircraft predicted ahead of schedule, not actionable."""
    new = _snapshot(inbound={"predicted_delay_minutes": -10})
    events = detect_wake_events(prev=None, new=new)
    assert not any(e["reason"] == "inbound_delay_predicted" for e in events)


def test_inbound_dedupe_boundary_is_inclusive():
    """Shift of exactly 5 min from a prior fired magnitude must NOT re-fire."""
    prev = _snapshot(inbound={"predicted_delay_minutes": 30})
    new = _snapshot(
        inbound={
            "predicted_delay_minutes": 30 + INBOUND_DELAY_DEDUPE_MINUTES,
            "predicted_time": "X",
        }
    )
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "inbound_delay_predicted" for e in events)


def test_inbound_dedupe_boundary_plus_one_re_fires():
    """Shift one minute beyond the dedupe boundary DOES re-fire."""
    prev = _snapshot(inbound={"predicted_delay_minutes": 30})
    new = _snapshot(
        inbound={
            "predicted_delay_minutes": 30 + INBOUND_DELAY_DEDUPE_MINUTES + 1,
            "predicted_time": "X",
        }
    )
    events = detect_wake_events(prev, new)
    inbound_events = [e for e in events if e["reason"] == "inbound_delay_predicted"]
    assert len(inbound_events) == 1


def test_inbound_threshold_crossing_within_dedupe_window_fires():
    """Prior below threshold (no fire) → new at/above threshold (within dedupe)
    must STILL fire. Dedupe only suppresses re-firing on prior values that
    themselves crossed the threshold."""
    prev = _snapshot(inbound={"predicted_delay_minutes": INBOUND_DELAY_THRESHOLD_MINUTES - 2})
    # Within dedupe of prev, but prev never fired (below threshold)
    new = _snapshot(
        inbound={
            "predicted_delay_minutes": INBOUND_DELAY_THRESHOLD_MINUTES + 1,
            "predicted_time": "2026-05-17T09:25:00-07:00",
        }
    )
    events = detect_wake_events(prev, new)
    inbound_events = [e for e in events if e["reason"] == "inbound_delay_predicted"]
    assert len(inbound_events) == 1
    assert inbound_events[0]["delay_minutes"] == INBOUND_DELAY_THRESHOLD_MINUTES + 1


# ---------------------------------------------------------------------------
# Carousel reveal
# ---------------------------------------------------------------------------


def test_carousel_reveal_fires_on_first_population():
    prev = _snapshot(baggage=None)
    new = _snapshot(baggage="CLM1")
    events = detect_wake_events(prev, new)
    assert {"reason": "carousel_revealed", "baggage": "CLM1"} in events


def test_carousel_already_assigned_does_not_re_fire():
    prev = _snapshot(baggage="CLM1")
    new = _snapshot(baggage="CLM1")
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "carousel_revealed" for e in events)


def test_carousel_first_cycle_already_populated_does_not_fire():
    """First-cycle baggage value is initial info; phase_markers handles
    arrival-logistics on its own time-based gate."""
    events = detect_wake_events(prev=None, new=_snapshot(baggage="CLM1"))
    assert not any(e["reason"] == "carousel_revealed" for e in events)


# ---------------------------------------------------------------------------
# Compound deltas (multiple events per call)
# ---------------------------------------------------------------------------


def test_gate_change_and_delay_both_fire():
    prev = _snapshot(dep_gate="B25", dep_time="2026-05-17T09:00:00-07:00")
    new = _snapshot(dep_gate="B7", dep_time="2026-05-17T10:00:00-07:00")
    events = detect_wake_events(prev, new)
    reasons = [e["reason"] for e in events]
    assert "gate_change" in reasons
    assert "delay" in reasons


def test_pure_function_no_state_mutation_on_inputs():
    """Calling detect_wake_events twice must produce the same output."""
    prev = _snapshot(dep_gate="B25")
    new = _snapshot(dep_gate="B7")
    first = detect_wake_events(prev, new)
    second = detect_wake_events(prev, new)
    assert first == second


# ---------------------------------------------------------------------------
# First-cycle schedule slip (#46) — a delay already baked into the first
# snapshot, detected as dep_time vs scheduled_dep_time (no prior to delta).
# ---------------------------------------------------------------------------


def test_first_cycle_schedule_slip_above_threshold_fires():
    """KL1017 (#46): first snapshot already sat at scheduled+31, delta rule
    had no prior to compare against, so no delay ever fired."""
    new = _snapshot(dep_time="2026-06-04T21:11:00+02:00")
    events = detect_wake_events(prev=None, new=new, scheduled_dep_time="2026-06-04T20:40:00+02:00")
    delay_events = [e for e in events if e["reason"] == "delay"]
    assert len(delay_events) == 1
    assert delay_events[0]["delay_minutes"] == 31
    assert delay_events[0]["schedule_slip"] is True
    assert delay_events[0]["new_dep_time"] == "2026-06-04T21:11:00+02:00"


def test_first_cycle_schedule_slip_at_threshold_fires():
    new = _snapshot(dep_time="2026-05-17T09:15:00-07:00")
    events = detect_wake_events(prev=None, new=new, scheduled_dep_time="2026-05-17T09:00:00-07:00")
    delay_events = [e for e in events if e["reason"] == "delay"]
    assert len(delay_events) == 1
    assert delay_events[0]["delay_minutes"] == DELAY_THRESHOLD_MINUTES


def test_first_cycle_schedule_slip_below_threshold_does_not_fire():
    new = _snapshot(dep_time="2026-05-17T09:14:00-07:00")  # 14 min
    events = detect_wake_events(prev=None, new=new, scheduled_dep_time="2026-05-17T09:00:00-07:00")
    assert not any(e["reason"] == "delay" for e in events)


def test_first_cycle_on_time_does_not_fire():
    new = _snapshot(dep_time="2026-05-17T09:00:00-07:00")
    events = detect_wake_events(prev=None, new=new, scheduled_dep_time="2026-05-17T09:00:00-07:00")
    assert not any(e["reason"] == "delay" for e in events)


def test_first_cycle_early_departure_does_not_fire():
    """An early first snapshot is not an actionable delay — only positive
    slips (departing later than scheduled) surface."""
    new = _snapshot(dep_time="2026-05-17T08:30:00-07:00")  # 30 min early
    events = detect_wake_events(prev=None, new=new, scheduled_dep_time="2026-05-17T09:00:00-07:00")
    assert not any(e["reason"] == "delay" for e in events)


def test_first_cycle_schedule_slip_without_scheduled_time_does_not_fire():
    """scheduled_dep_time absent (None) disables the first-cycle check
    gracefully rather than raising."""
    new = _snapshot(dep_time="2026-06-04T21:11:00+02:00")
    events = detect_wake_events(prev=None, new=new, scheduled_dep_time=None)
    assert not any(e["reason"] == "delay" for e in events)


def test_persistent_schedule_slip_does_not_re_fire_once_prev_exists():
    """The slip surfaces once on the first cycle; subsequent polls carry the
    same slipped dep_time, so the delta rule sees no change and the
    first-cycle branch is gated off — no re-fire each poll."""
    slipped = _snapshot(dep_time="2026-06-04T21:11:00+02:00")
    events = detect_wake_events(
        prev=slipped, new=slipped, scheduled_dep_time="2026-06-04T20:40:00+02:00"
    )
    assert not any(e["reason"] == "delay" for e in events)


# ---------------------------------------------------------------------------
# Inbound delay retraction (#48) — a previously-surfaced prediction that
# walks back below threshold or to null fires an all-clear.
# ---------------------------------------------------------------------------


def test_inbound_retraction_to_null_fires():
    """DL59 (#48): inbound escalated to 'rebook now', then predicted_delay
    retracted to null — without an all-clear the last surface stayed
    'rebook now'."""
    prev = _snapshot(inbound={"predicted_delay_minutes": 95})
    new = _snapshot(inbound={"predicted_delay_minutes": None})
    events = detect_wake_events(prev, new)
    retractions = [e for e in events if e["reason"] == "inbound_delay_retracted"]
    assert len(retractions) == 1
    assert retractions[0]["prev_delay_minutes"] == 95
    assert retractions[0]["new_delay_minutes"] is None


def test_inbound_retraction_below_threshold_fires():
    prev = _snapshot(inbound={"predicted_delay_minutes": 95})
    new = _snapshot(inbound={"predicted_delay_minutes": INBOUND_DELAY_THRESHOLD_MINUTES - 5})
    events = detect_wake_events(prev, new)
    retractions = [e for e in events if e["reason"] == "inbound_delay_retracted"]
    assert len(retractions) == 1
    assert retractions[0]["new_delay_minutes"] == INBOUND_DELAY_THRESHOLD_MINUTES - 5


def test_inbound_retraction_when_inbound_block_absent_fires():
    """Inbound block dropping out entirely (None) is also a retraction."""
    prev = _snapshot(inbound={"predicted_delay_minutes": 44})
    new = _snapshot(inbound=None)
    events = detect_wake_events(prev, new)
    retractions = [e for e in events if e["reason"] == "inbound_delay_retracted"]
    assert len(retractions) == 1
    assert retractions[0]["prev_delay_minutes"] == 44
    assert retractions[0]["new_delay_minutes"] is None


def test_inbound_still_above_threshold_does_not_retract():
    """A partial walk-back that stays at/above threshold is still a delay,
    not an all-clear."""
    prev = _snapshot(inbound={"predicted_delay_minutes": 95})
    new = _snapshot(inbound={"predicted_delay_minutes": 80, "predicted_time": "X"})
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "inbound_delay_retracted" for e in events)


def test_inbound_retraction_requires_prior_above_threshold():
    """If we never surfaced a delay (prior below threshold), there is nothing
    to retract."""
    prev = _snapshot(inbound={"predicted_delay_minutes": INBOUND_DELAY_THRESHOLD_MINUTES - 5})
    new = _snapshot(inbound={"predicted_delay_minutes": None})
    events = detect_wake_events(prev, new)
    assert not any(e["reason"] == "inbound_delay_retracted" for e in events)


def test_inbound_retraction_not_fired_on_first_cycle():
    """No prior prediction (prev is None) means nothing was surfaced to
    retract."""
    new = _snapshot(inbound={"predicted_delay_minutes": None})
    events = detect_wake_events(prev=None, new=new)
    assert not any(e["reason"] == "inbound_delay_retracted" for e in events)


def test_inbound_retraction_and_prediction_mutually_exclusive():
    """A retraction never coincides with a fresh prediction in the same call."""
    prev = _snapshot(inbound={"predicted_delay_minutes": 95})
    new = _snapshot(inbound={"predicted_delay_minutes": None})
    events = detect_wake_events(prev, new)
    reasons = [e["reason"] for e in events]
    assert "inbound_delay_retracted" in reasons
    assert "inbound_delay_predicted" not in reasons
