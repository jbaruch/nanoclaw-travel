"""Tests for the flight disposition resolver (`disposition.py`).

Deterministic fixtures only — fixed timestamps and hand-built state
records, no generated inputs. `now` is a fixed instant so the
past/future boundary is unambiguous.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from calendar_plan import (  # noqa: E402
    DISPOSITION_ACTIVE,
    DISPOSITION_CANCELLED,
    DISPOSITION_COMPLETED,
    DISPOSITION_DIVERTED,
    DISPOSITION_SWITCHED_AWAY,
)
from disposition import DispositionError, resolve_disposition  # noqa: E402

# Fixed wall clock for every case. Flights scheduled before this are past,
# after it are future.
NOW = datetime(2026, 5, 17, 18, 0, 0, tzinfo=timezone.utc)

FUTURE_ARR = "2026-05-17T22:00:00+00:00"  # 4h after NOW
PAST_ARR = "2026-05-17T14:00:00+00:00"  # 4h before NOW


def _state(*, scheduled_arr=FUTURE_ARR, snapshot=None):
    """Build a minimal per-flight state record for the resolver."""
    state = {"flight_id": 100, "scheduled_arr_time": scheduled_arr}
    if snapshot is not None:
        state["last_snapshot"] = snapshot
    return state


# --- status-driven (precedence over membership + time) -------------------


def test_cancelled_status_wins_even_when_active_and_future():
    state = _state(snapshot={"computed_status": "cancelled"})
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_CANCELLED


def test_diverted_status_wins_even_when_active_and_future():
    state = _state(snapshot={"computed_status": "diverted"})
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_DIVERTED


def test_landed_status_is_completed_even_if_arrival_still_future():
    # byAir can flip to landed slightly before the scheduled arrival passes;
    # the status is authoritative for completion.
    state = _state(scheduled_arr=FUTURE_ARR, snapshot={"computed_status": "landed"})
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_COMPLETED


# --- time-driven completion ----------------------------------------------


def test_past_arrival_is_completed_when_active():
    state = _state(scheduled_arr=PAST_ARR, snapshot={"computed_status": "en_route"})
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_COMPLETED


def test_past_arrival_is_completed_when_not_active():
    # A switched-away flight whose time has passed is a historical record,
    # not a teardown target — completion (time) outranks switched_away.
    state = _state(scheduled_arr=PAST_ARR)
    assert resolve_disposition(state, in_active_flights=False, now=NOW) == DISPOSITION_COMPLETED


def test_actual_arrival_overrides_scheduled_for_completion():
    # Scheduled arrival is still future, but byAir published an actual
    # arrival in the past (flight arrived early) — completed.
    state = _state(scheduled_arr=FUTURE_ARR, snapshot={"arr_time": PAST_ARR})
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_COMPLETED


def test_actual_arrival_future_keeps_active_despite_past_scheduled():
    # Delayed flight: scheduled arrival passed, but the actual arrival byAir
    # now shows is still ahead — the flight has not landed, stays active.
    state = _state(scheduled_arr=PAST_ARR, snapshot={"arr_time": FUTURE_ARR})
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_ACTIVE


def test_arrival_exactly_now_is_completed():
    now_iso = NOW.isoformat()
    state = _state(scheduled_arr=now_iso)
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_COMPLETED


# --- membership-driven ----------------------------------------------------


def test_future_and_active_is_active():
    state = _state(scheduled_arr=FUTURE_ARR, snapshot={"computed_status": "scheduled"})
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_ACTIVE


def test_future_and_not_active_is_switched_away():
    state = _state(scheduled_arr=FUTURE_ARR, snapshot={"computed_status": "scheduled"})
    assert resolve_disposition(state, in_active_flights=False, now=NOW) == DISPOSITION_SWITCHED_AWAY


def test_no_snapshot_future_active_is_active():
    # last_snapshot is null before the first byAir fetch — a brand-new
    # future flight with no snapshot is still active.
    state = _state(scheduled_arr=FUTURE_ARR, snapshot=None)
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_ACTIVE


def test_null_snapshot_is_tolerated():
    state = {"flight_id": 100, "scheduled_arr_time": FUTURE_ARR, "last_snapshot": None}
    assert resolve_disposition(state, in_active_flights=False, now=NOW) == DISPOSITION_SWITCHED_AWAY


# --- error handling -------------------------------------------------------


def test_naive_now_raises():
    state = _state()
    with pytest.raises(DispositionError, match="timezone-aware"):
        resolve_disposition(state, in_active_flights=True, now=datetime(2026, 5, 17, 18, 0, 0))


def test_missing_arrival_fields_raises():
    state = {"flight_id": 100}  # no scheduled_arr_time, no snapshot
    with pytest.raises(DispositionError, match="cannot determine"):
        resolve_disposition(state, in_active_flights=True, now=NOW)


def test_naive_arrival_string_raises():
    state = _state(scheduled_arr="2026-05-17T22:00:00")  # no offset
    with pytest.raises(DispositionError, match="missing a UTC offset"):
        resolve_disposition(state, in_active_flights=True, now=NOW)


def test_empty_actual_arrival_fails_loudly_not_silent_fallback():
    # A present-but-empty last_snapshot.arr_time is malformed; it must
    # raise the parse error, not silently fall back to scheduled.
    state = _state(scheduled_arr=FUTURE_ARR, snapshot={"arr_time": ""})
    with pytest.raises(DispositionError, match="last_snapshot.arr_time"):
        resolve_disposition(state, in_active_flights=True, now=NOW)


def test_empty_scheduled_arrival_fails_loudly():
    state = _state(scheduled_arr="")
    with pytest.raises(DispositionError, match="scheduled_arr_time"):
        resolve_disposition(state, in_active_flights=True, now=NOW)


def test_non_utc_offset_arrival_normalized_correctly():
    # -07:00 arrival at 15:00 == 22:00 UTC, still future relative to NOW.
    state = _state(scheduled_arr="2026-05-17T15:00:00-07:00")
    assert resolve_disposition(state, in_active_flights=True, now=NOW) == DISPOSITION_ACTIVE
