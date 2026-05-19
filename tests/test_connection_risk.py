"""Tests for skills/flight-assist/connection_risk.py.

Pure-function tests with synthetic per-flight state fixtures. Each
test builds the minimal pair (or trio) of state records needed to
exercise one branch — independent fixtures per test, per
`coding-policy: testing-standards` "Independence".
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from connection_risk import (  # noqa: E402
    DEFAULT_MIN_TRANSFER_MINUTES,
    detect_connection_risks,
)


def _state(
    *,
    flight_id: int,
    trip_id: int,
    code: str,
    dep_airport_id: int,
    arr_airport_id: int,
    scheduled_dep_time: str,
    scheduled_arr_time: str,
    snapshot: dict | None = None,
    marker_fired: bool = False,
) -> dict:
    """Build a synthetic per-flight state record matching state-schema.md."""
    return {
        "schema_version": 2,
        "flight_id": flight_id,
        "code": code,
        "ownership": "mine",
        "trip_id": trip_id,
        "scheduled_dep_time": scheduled_dep_time,
        "scheduled_arr_time": scheduled_arr_time,
        "dep_airport_id": dep_airport_id,
        "arr_airport_id": arr_airport_id,
        "last_polled_at": "2026-05-17T12:00:00Z",
        "last_snapshot": snapshot,
        "phase_markers": {
            "day_before_fired": False,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": marker_fired,
        },
        "last_wake_at": None,
        "last_wake_reason": None,
    }


def _snapshot(**overrides) -> dict:
    base = {
        "code": "XX123",
        "computed_status": "scheduled",
        "dep_gate": None,
        "arr_gate": None,
        "dep_time": None,
        "arr_time": None,
        "baggage": None,
        "inbound": None,
    }
    base.update(overrides)
    return base


_NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Single-flight + no-connection cases (no events)
# ---------------------------------------------------------------------------


def test_single_flight_trip_no_events():
    """One flight per trip means no connection to evaluate."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
        )
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert events == []


def test_empty_state_list_no_events():
    events = detect_connection_risks(flight_states=[], now_utc=_NOW)
    assert events == []


def test_two_legs_different_airports_no_events():
    """Open-jaw: leg-1 lands at 28, leg-2 departs from 30 — not a connection."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=30,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:30:00-07:00",
            scheduled_arr_time="2026-05-17T18:30:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert events == []


def test_trip_id_zero_excluded():
    """trip_id=0 is the sync fallback for missing — excluded from grouping."""
    states = [
        _state(
            flight_id=1,
            trip_id=0,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
        ),
        _state(
            flight_id=2,
            trip_id=0,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T15:30:00-07:00",
            scheduled_arr_time="2026-05-17T17:30:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert events == []


# ---------------------------------------------------------------------------
# Tight-connection firing
# ---------------------------------------------------------------------------


def test_tight_connection_fires_when_window_below_default():
    """Scheduled 70-min layover but leg-1 delayed; window is 30 min < 45 default."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=_snapshot(
                computed_status="departed",
                arr_time="2026-05-17T15:30:00-07:00",  # 30 min delay
            ),
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:00:00-07:00",
            scheduled_arr_time="2026-05-17T18:00:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert len(events) == 1
    flight_id, event = events[0]
    assert flight_id == 2
    assert event["reason"] == "connection_at_risk"
    assert event["transfer_minutes_remaining"] == 30
    assert event["scheduled_layover_minutes"] == 60
    assert event["min_transfer_minutes"] == DEFAULT_MIN_TRANSFER_MINUTES
    assert event["connecting_airport_id"] == 28
    assert event["leg1_code"] == "AA100"
    assert event["leg2_code"] == "AA200"
    assert event["leg1_flight_id"] == 1


def test_comfortable_layover_does_not_fire():
    """Scheduled 90-min layover, no leg-1 delay — well above 45-min default."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:30:00-07:00",
            scheduled_arr_time="2026-05-17T18:30:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert events == []


def test_fires_using_scheduled_arr_when_snapshot_missing():
    """First cycle: no snapshot yet, scheduled times alone show tight window."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=None,
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            # 30 min scheduled layover — below 45 default
            scheduled_dep_time="2026-05-17T15:30:00-07:00",
            scheduled_arr_time="2026-05-17T17:30:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert len(events) == 1
    assert events[0][1]["transfer_minutes_remaining"] == 30


def test_overridden_min_transfer_minutes():
    """Caller-supplied threshold suppresses what the default would fire."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            # 40 min layover, default would fire; with 30 it should not
            scheduled_dep_time="2026-05-17T15:40:00-07:00",
            scheduled_arr_time="2026-05-17T17:40:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW, min_transfer_minutes=30)
    assert events == []


# ---------------------------------------------------------------------------
# Suppression rules
# ---------------------------------------------------------------------------


def test_marker_already_fired_does_not_re_fire():
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=_snapshot(
                computed_status="departed",
                arr_time="2026-05-17T15:30:00-07:00",
            ),
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:00:00-07:00",
            scheduled_arr_time="2026-05-17T18:00:00-07:00",
            marker_fired=True,
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert events == []


def test_leg1_landed_does_not_fire():
    """Once leg-1 has landed, the outcome is observable — no more alerts."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=_snapshot(
                computed_status="landed",
                arr_time="2026-05-17T15:30:00-07:00",
            ),
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:00:00-07:00",
            scheduled_arr_time="2026-05-17T18:00:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert events == []


def test_leg1_cancelled_does_not_fire_connection_risk():
    """The cancel alert is the actionable signal — don't double up."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=_snapshot(computed_status="cancelled"),
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T15:30:00-07:00",
            scheduled_arr_time="2026-05-17T17:30:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert events == []


def test_leg1_diverted_does_not_fire_connection_risk():
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=_snapshot(computed_status="diverted"),
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T15:30:00-07:00",
            scheduled_arr_time="2026-05-17T17:30:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert events == []


def test_leg1_more_than_24h_away_does_not_fire():
    """Delay projections > 24h out are speculative; don't fire."""
    far_now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)  # ~26h before dep
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T14:00:00-07:00",  # 14:00 PDT = 21:00 UTC
            scheduled_arr_time="2026-05-17T16:00:00-07:00",
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:30:00-07:00",
            scheduled_arr_time="2026-05-17T18:30:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=far_now)
    assert events == []


# ---------------------------------------------------------------------------
# Multi-leg trip
# ---------------------------------------------------------------------------


def test_three_leg_trip_evaluates_each_pair():
    """leg-1→leg-2 and leg-2→leg-3 both evaluated, only the tight pair fires."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            # 90-min comfortable layover from leg-1
            scheduled_dep_time="2026-05-17T16:30:00-07:00",
            scheduled_arr_time="2026-05-17T18:30:00-07:00",
        ),
        _state(
            flight_id=3,
            trip_id=100,
            code="AA300",
            dep_airport_id=40,
            arr_airport_id=50,
            # 30-min tight layover from leg-2
            scheduled_dep_time="2026-05-17T19:00:00-07:00",
            scheduled_arr_time="2026-05-17T20:30:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert len(events) == 1
    assert events[0][0] == 3  # leg-3 is the at-risk downstream leg
    assert events[0][1]["leg1_code"] == "AA200"
    assert events[0][1]["leg2_code"] == "AA300"


def test_unsorted_input_groups_by_dep_time_within_trip():
    """Caller may pass states in any order; module sorts by scheduled_dep_time."""
    leg2 = _state(
        flight_id=2,
        trip_id=100,
        code="AA200",
        dep_airport_id=28,
        arr_airport_id=40,
        scheduled_dep_time="2026-05-17T15:30:00-07:00",
        scheduled_arr_time="2026-05-17T17:30:00-07:00",
    )
    leg1 = _state(
        flight_id=1,
        trip_id=100,
        code="AA100",
        dep_airport_id=20,
        arr_airport_id=28,
        scheduled_dep_time="2026-05-17T13:00:00-07:00",
        scheduled_arr_time="2026-05-17T15:00:00-07:00",
    )
    events = detect_connection_risks(flight_states=[leg2, leg1], now_utc=_NOW)
    assert len(events) == 1
    assert events[0][1]["leg1_code"] == "AA100"
    assert events[0][1]["leg2_code"] == "AA200"


# ---------------------------------------------------------------------------
# Missed connection (transfer window <= 0)
# ---------------------------------------------------------------------------


def test_missed_connection_flag_when_window_is_negative():
    """Projected leg-1 arrival AFTER leg-2 dep produces missed_connection: true."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=_snapshot(
                computed_status="en_route",
                # Live ETA pushed to 30 min AFTER leg-2 scheduled dep
                arr_time="2026-05-17T16:30:00-07:00",
            ),
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:00:00-07:00",
            scheduled_arr_time="2026-05-17T18:00:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert len(events) == 1
    event = events[0][1]
    assert event["missed_connection"] is True
    assert event["transfer_minutes_remaining"] == -30


def test_missed_connection_false_when_window_positive():
    """missed_connection is false for normal tight-but-positive windows."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=_snapshot(
                computed_status="departed",
                arr_time="2026-05-17T15:30:00-07:00",
            ),
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:00:00-07:00",
            scheduled_arr_time="2026-05-17T18:00:00-07:00",
        ),
    ]
    events = detect_connection_risks(flight_states=states, now_utc=_NOW)
    event = events[0][1]
    assert event["missed_connection"] is False


# ---------------------------------------------------------------------------
# Multi-timezone trip sorting
# ---------------------------------------------------------------------------


def test_multi_timezone_sort_uses_utc_not_string():
    """JST leg in the morning sorts before PDT leg in the morning of the same date.

    "2026-05-17T08:00:00+09:00" (JST = 23:00 UTC on May 16) and
    "2026-05-17T10:00:00-07:00" (PDT = 17:00 UTC on May 17) — naive string
    sort would put JST first but UTC sort agrees (JST is actually
    earlier). The opposite case proves the test: an evening JST leg
    that string-sorts AFTER a morning PDT leg is actually earlier.
    """
    pdt_leg = _state(
        flight_id=1,
        trip_id=100,
        code="USJP1",
        dep_airport_id=20,
        arr_airport_id=28,
        # PDT 09:00 = UTC 16:00 (later in UTC than the JST leg below)
        scheduled_dep_time="2026-05-17T09:00:00-07:00",
        scheduled_arr_time="2026-05-17T11:00:00-07:00",
    )
    jst_leg = _state(
        flight_id=2,
        trip_id=100,
        code="USJP2",
        dep_airport_id=28,
        arr_airport_id=40,
        # JST 23:00 on May 17 = UTC 14:00 on May 17 — EARLIER in UTC
        # than the PDT 09:00 = UTC 16:00 above, despite the local time
        # string ordering suggesting the opposite.
        scheduled_dep_time="2026-05-17T23:00:00+09:00",
        scheduled_arr_time="2026-05-18T01:00:00+09:00",
    )
    # Caller passes them in the "wrong" order — sort must normalize via UTC.
    groups = detect_connection_risks(flight_states=[pdt_leg, jst_leg], now_utc=_NOW)
    # No connection emitted (airports don't chain), but the relevant
    # assertion is that the sort didn't crash and grouping found the
    # pair. To test the sort directly, exercise _group_by_trip via the
    # public API: the function returns [] here because no airport
    # matches, but it ran without error and reached the inner pair walk.
    assert groups == []


# ---------------------------------------------------------------------------
# Input independence
# ---------------------------------------------------------------------------


def test_pure_function_does_not_mutate_input_states():
    """Calling twice produces identical output and leaves inputs untouched."""
    states = [
        _state(
            flight_id=1,
            trip_id=100,
            code="AA100",
            dep_airport_id=20,
            arr_airport_id=28,
            scheduled_dep_time="2026-05-17T13:00:00-07:00",
            scheduled_arr_time="2026-05-17T15:00:00-07:00",
            snapshot=_snapshot(computed_status="departed", arr_time="2026-05-17T15:30:00-07:00"),
        ),
        _state(
            flight_id=2,
            trip_id=100,
            code="AA200",
            dep_airport_id=28,
            arr_airport_id=40,
            scheduled_dep_time="2026-05-17T16:00:00-07:00",
            scheduled_arr_time="2026-05-17T18:00:00-07:00",
        ),
    ]
    # Deep copy so the comparison detects mutation of nested dicts
    # (phase_markers, last_snapshot), not just top-level rebinds.
    before = copy.deepcopy(states)
    first = detect_connection_risks(flight_states=states, now_utc=_NOW)
    second = detect_connection_risks(flight_states=states, now_utc=_NOW)
    assert first == second
    assert states == before
