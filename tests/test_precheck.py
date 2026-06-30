"""Tests for skills/flight-assist/precheck.py.

End-to-end orchestration tests with mocked ByAirClient + MapsClient
+ tmp state dir, exercising the precheck contract documented in
SKILL.md (forthcoming PR). The tests run the same `_run_cycle()`
entry-point the script invokes but bypass the outer-boundary catch
so a programming bug in the modules under test surfaces directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

import precheck  # noqa: E402
from state import (  # noqa: E402
    CURRENT_LOCATION_FILE,
    CURRENT_LOCATION_SCHEMA_VERSION,
    read_flight_state,
    write_active_flights,
    write_flight_state,
)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "state" / "flight-assist"
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(root))
    monkeypatch.setenv("BYAIR_MCP_URL", "https://api.byairapp.example/mcp?api_key=test")
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    return root


def _byair_flight(
    *,
    flight_id: int = 12345,
    computed_status: str = "scheduled",
    dep_gate: str | None = None,
    dep_terminal: str | None = None,
    dep_time: str | None = "2026-05-18T17:00:00+00:00",
    baggage: str | None = None,
    inbound: dict | None = None,
) -> dict:
    """Build the raw shape get_flight() would return."""
    return {
        "id": flight_id,
        "code": "XX123",
        "computed_status": computed_status,
        "computed_status_detail": "...",
        "computed_phase_progress": None,
        "computed_phase_risk": None,
        "computed_phase_overdue": None,
        "depGate": dep_gate,
        "arrGate": None,
        "depTerminal": dep_terminal,
        "arrTerminal": None,
        "depTime": dep_time,
        "arrTime": None,
        "scheduledDepTime": "2026-05-18T17:00:00+00:00",
        "scheduledArrTime": "2026-05-18T20:00:00+00:00",
        "baggage": baggage,
        "inbound": inbound or {},
        "position": {"currentPosition": {}},
        "depAirport": {"id": 20, "name": "San Francisco International Airport"},
        "arrAirport": {"id": 28, "name": "Phoenix"},
        "trip_id": 678,
    }


def _scheduled_snapshot(*, code: str = "XX123") -> dict:
    """Minimal scheduled-status snapshot for tests asserting cadence-gate behavior."""
    return {
        "code": code,
        "computed_status": "scheduled",
        "computed_status_detail": "...",
        "computed_phase_progress": None,
        "computed_phase_risk": None,
        "computed_phase_overdue": None,
        "dep_gate": None,
        "arr_gate": None,
        "dep_terminal": None,
        "arr_terminal": None,
        "dep_time": "2026-05-18T17:00:00+00:00",
        "arr_time": None,
        "baggage": None,
        "inbound": {
            "aircraft_model": None,
            "registration": None,
            "flew": None,
            "predicted_delay_minutes": None,
        },
        "position_lat": None,
        "position_lon": None,
    }


def _make_state(flight_id: int = 12345, **overrides) -> dict:
    base = {
        "flight_id": flight_id,
        "code": "XX123",
        "ownership": "mine",
        "trip_id": 678,
        "scheduled_dep_time": "2026-05-18T17:00:00+00:00",
        "scheduled_arr_time": "2026-05-18T20:00:00+00:00",
        "dep_airport_id": 20,
        "arr_airport_id": 28,
        "last_polled_at": "2026-05-18T16:00:00Z",
        "last_snapshot": None,
        "phase_markers": {
            "day_before_fired": False,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": False,
        },
        "last_wake_at": None,
        "last_wake_reason": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Origin-resolution ladder (issue #18)
# ---------------------------------------------------------------------------


def _write_current_location(
    state_root: Path,
    *,
    latitude: float = 59.6519,
    longitude: float = 17.9186,
    captured_at: str,
) -> None:
    """Write a valid host-owned location snapshot to the per-test
    state dir. Stamps the canonical `schema_version` so the non-owner
    reader gate in `state.read_current_location` accepts the payload —
    host-side writes carry this field per state-schema.md. Filename is
    sourced from the production constant so a tile-side rename can't
    leave these fixtures pointing at the old path."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / CURRENT_LOCATION_FILE).write_text(
        json.dumps(
            {
                "schema_version": CURRENT_LOCATION_SCHEMA_VERSION,
                "latitude": latitude,
                "longitude": longitude,
                "captured_at": captured_at,
            }
        )
    )


def test_origin_ladder_prefers_fresh_current_location(state_root: Path):
    """Fresh `current-location.json` (≤ 30 min old) wins over
    `home_address` — the time-to-leave query uses the live coordinates
    formatted as `"lat,lng"` (the Distance Matrix API accepts the
    numeric pair natively)."""
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    _write_current_location(state_root, captured_at="2026-05-20T11:40:00Z")
    origin = precheck._resolve_time_to_leave_origin(
        home_address="1 Infinite Loop, Cupertino, CA", now_utc=now
    )
    assert origin == "59.6519,17.9186"


def test_origin_ladder_falls_back_to_home_when_location_stale(state_root: Path):
    """A stale snapshot (older than `_MAX_CURRENT_LOCATION_AGE_MINUTES`)
    is ignored; the precheck falls back to `home_address`."""
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    _write_current_location(state_root, captured_at="2026-05-20T11:00:00Z")  # 60 min old
    origin = precheck._resolve_time_to_leave_origin(
        home_address="1 Infinite Loop, Cupertino, CA", now_utc=now
    )
    assert origin == "1 Infinite Loop, Cupertino, CA"


def test_origin_ladder_ignores_future_captured_at(state_root: Path):
    """A `captured_at` later than `now_utc` is rejected as untrusted
    (clock skew / corruption) — fall back to `home_address` rather
    than honour a snapshot from the future."""
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    _write_current_location(state_root, captured_at="2026-05-20T13:00:00Z")
    origin = precheck._resolve_time_to_leave_origin(
        home_address="1 Infinite Loop, Cupertino, CA", now_utc=now
    )
    assert origin == "1 Infinite Loop, Cupertino, CA"


def test_origin_ladder_falls_back_to_home_when_location_missing(state_root: Path):
    """No `current-location.json` on disk → `home_address` is the
    origin (today's behaviour, preserved)."""
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    origin = precheck._resolve_time_to_leave_origin(
        home_address="1 Infinite Loop, Cupertino, CA", now_utc=now
    )
    assert origin == "1 Infinite Loop, Cupertino, CA"


def test_origin_ladder_returns_none_when_no_origin(state_root: Path):
    """No location AND no home_address → None; the caller skips the
    maps query entirely. Today's silent-failure mode for a user who
    completed neither `/setup` nor live-location sharing."""
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    origin = precheck._resolve_time_to_leave_origin(home_address=None, now_utc=now)
    assert origin is None


# ---------------------------------------------------------------------------
# Cycle-level tests
# ---------------------------------------------------------------------------


def test_no_active_flights_yields_no_events(state_root: Path):
    """Empty active-flights index returns []."""
    write_active_flights([])
    with patch("precheck.ByAirClient.from_env"):
        events = precheck._run_cycle(now_utc=datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc))
    assert events == []


def test_run_cycle_passes_bounded_per_call_timeout_to_byair_client(state_root: Path):
    """`_run_cycle` must instantiate ByAirClient with the bounded per-call timeout.

    The 8s cap is the load-bearing fix for #28 — without it the outer 30s
    execFile budget races the client's default 30s and the whole cycle dies
    as execfile-error. Pin the value here so a future refactor cannot
    silently drop or rename the kwarg.
    """
    write_active_flights([])
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        precheck._run_cycle(now_utc=datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc))
    mock_byair_from_env.assert_called_once_with(timeout=precheck._BYAIR_CALL_TIMEOUT_SECONDS)
    assert precheck._BYAIR_CALL_TIMEOUT_SECONDS == 8.0


def test_run_cycle_passes_bounded_per_call_timeout_to_maps_client(state_root: Path, monkeypatch):
    """`_run_cycle` must instantiate MapsClient with the bounded per-call timeout.

    The Maps query runs inside `_process_flight` stacked on the byAir poll. The
    MapsClient default is 10s, which (added to the byAir 8s) overran the 30s
    agent-runner hard-kill and surfaced as execfile-error
    (jbaruch/nanoclaw#562). Pin the kwarg + value so a refactor can't silently
    drop it.
    """
    write_active_flights([])
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "synthetic-key")
    with patch("precheck.MapsClient.from_env") as mock_maps_from_env:
        precheck._run_cycle(now_utc=datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc))
    mock_maps_from_env.assert_called_once_with(timeout=precheck._MAPS_CALL_TIMEOUT_SECONDS)
    assert precheck._MAPS_CALL_TIMEOUT_SECONDS == 8.0


def test_poll_headroom_covers_byair_plus_maps_worst_case():
    """Regression guard for the execfile-error hard-kill (jbaruch/nanoclaw#562).

    A single `_process_flight` does one byAir poll AND one Maps query. The
    poll-loop headroom reserved before the 30s kill must cover BOTH, or a
    flight started just under the budget overruns and the whole cycle is
    killed. The earlier headroom (10s) only covered the byAir poll.
    """
    assert (
        precheck._CYCLE_POLL_HEADROOM_SECONDS
        >= precheck._BYAIR_CALL_TIMEOUT_SECONDS + precheck._MAPS_CALL_TIMEOUT_SECONDS
    )
    # The budget must still leave positive time to start at least one poll.
    assert precheck._CYCLE_WALL_CLOCK_BUDGET_SECONDS > 0


def test_first_cycle_for_new_flight_writes_state_and_fires_day_before(state_root: Path):
    """First poll past T-24h: write snapshot AND fire the day_before event."""
    write_active_flights([12345])
    fake_flight = _byair_flight(flight_id=12345)

    fake_now = datetime(
        2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc
    )  # T-1h, well past day-before window
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)

    # Multiple events expected: day_before (T-1h, well past T-24h) +
    # arrival_logistics (T-arr-15min check at T-arr-4h = false; not fired)
    persisted = read_flight_state(12345)
    assert persisted is not None
    assert persisted["last_snapshot"]["computed_status"] == "scheduled"
    # day_before should fire on first cycle since we're past T-24h
    reasons = [e["event"]["reason"] for e in events]
    assert "day_before" in reasons


def test_gate_change_fires_after_readout(state_root: Path):
    """After the gate_assignment readout has fired, a later gate move surfaces
    as gate_change (#103 acceptance: gate change after the readout, in-window)."""
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T15:00:00Z",
        last_snapshot={
            "code": "XX123",
            "computed_status": "scheduled",
            "computed_status_detail": "...",
            "computed_phase_progress": None,
            "computed_phase_risk": None,
            "computed_phase_overdue": None,
            "dep_gate": "B25",
            "arr_gate": None,
            "dep_terminal": None,
            "arr_terminal": None,
            "dep_time": "2026-05-18T17:00:00+00:00",
            "arr_time": None,
            "baggage": None,
            "inbound": {
                "aircraft_model": None,
                "registration": None,
                "flew": None,
                "predicted_delay_minutes": None,
            },
            "position_lat": None,
            "position_lon": None,
        },
        phase_markers={
            "day_before_fired": True,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": True,  # readout already done
        },
    )
    write_flight_state(prior)
    write_active_flights([12345])

    fake_flight = _byair_flight(flight_id=12345, dep_gate="B7")
    fake_now = datetime(
        2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc
    )  # past 10-min cadence (last_polled was 15:00)

    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)

    reasons = [e["event"]["reason"] for e in events]
    assert "gate_change" in reasons


def _scheduled_snapshot_with_gate(*, dep_gate: str | None) -> dict:
    snap = _scheduled_snapshot()
    snap["dep_gate"] = dep_gate
    return snap


def test_gate_change_suppressed_before_readout_window(state_root: Path):
    """#103 acceptance: a gate present/changing at T−3h emits no notification,
    but the snapshot (new gate) is still written to state."""
    prior = _make_state(
        flight_id=12345,
        # T−3h relative to 17:00 dep; narrowbody window opens at 15:30, so 14:00
        # is well before it. Last polled 30 min earlier to clear the cadence gate.
        last_polled_at="2026-05-18T13:30:00Z",
        last_snapshot=_scheduled_snapshot_with_gate(dep_gate="D3"),
        phase_markers={
            "day_before_fired": True,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": False,
        },
    )
    write_flight_state(prior)
    write_active_flights([12345])

    fake_flight = _byair_flight(flight_id=12345, dep_gate="D1")  # gate churned D3 → D1
    fake_now = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)  # before window

    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)

    reasons = [e["event"]["reason"] for e in events]
    assert "gate_change" not in reasons
    assert "gate_assignment" not in reasons  # window not open yet
    # State still records the latest gate.
    persisted = read_flight_state(12345)
    assert persisted["last_snapshot"]["dep_gate"] == "D1"


def test_gate_assignment_readout_fires_in_window(state_root: Path):
    """#103 acceptance: window opens with a gate present → one gate_assignment
    readout carrying gate + terminal, and the simultaneous gate delta is muted."""
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T15:20:00Z",
        last_snapshot=_scheduled_snapshot_with_gate(dep_gate="D3"),
        phase_markers={
            "day_before_fired": True,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": False,
        },
    )
    write_flight_state(prior)
    write_active_flights([12345])

    # New gate D6 with terminal "1"; window opens 15:30 (narrowbody), now 15:40.
    fake_flight = _byair_flight(flight_id=12345, dep_gate="D6", dep_terminal="1")
    fake_now = datetime(2026, 5, 18, 15, 40, 0, tzinfo=timezone.utc)

    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)

    by_reason = {e["event"]["reason"]: e["event"] for e in events}
    assert "gate_assignment" in by_reason
    assert by_reason["gate_assignment"]["dep_gate"] == "D6"
    assert by_reason["gate_assignment"]["dep_terminal"] == "1"
    assert "gate_change" not in by_reason  # muted on the readout cycle
    # Marker persisted so the next gate move surfaces as gate_change.
    persisted = read_flight_state(12345)
    assert persisted["phase_markers"]["gate_assignment_fired"] is True


def test_gate_assignment_window_resolves_widebody_lead_from_snapshot(state_root: Path):
    """End-to-end: a widebody inbound aircraft resolves a 50-min boarding lead
    through the real precheck path, opening the readout window 20 min earlier
    than the narrowbody default. The readout fires at 15:15 — inside the
    widebody window (opens 15:10) but before a narrowbody flight's window
    (opens 15:30), so firing here proves the precheck resolved the 50-min lead
    from the snapshot, not the 30-min fallback. The top-level-model and
    transoceanic inputs are not yet stamped into the snapshot (#55); the
    inbound-aircraft chain is the path available today."""
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T14:55:00Z",
        last_snapshot=_scheduled_snapshot_with_gate(dep_gate=None),
        phase_markers={
            "day_before_fired": True,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": False,
        },
    )
    write_flight_state(prior)
    write_active_flights([12345])

    fake_flight = _byair_flight(
        flight_id=12345,
        dep_gate="E16",
        dep_terminal="2",
        inbound={"aircraft_model": "Boeing 777-300ER"},  # widebody → 50-min lead
    )
    fake_now = datetime(2026, 5, 18, 15, 15, 0, tzinfo=timezone.utc)

    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)

    by_reason = {e["event"]["reason"]: e["event"] for e in events}
    assert "gate_assignment" in by_reason  # would NOT fire on a 30-min lead at 15:15
    assert by_reason["gate_assignment"]["dep_terminal"] == "2"


def test_readout_cycle_drops_dep_gate_change_but_keeps_arr(state_root: Path):
    """#103: on the readout's own cycle, the redundant DEParture gate_change is
    dropped (the gate_assignment carries the dep gate), but a simultaneous
    ARRival gate change still surfaces — the readout says nothing about arr."""
    prior_snapshot = _scheduled_snapshot()
    prior_snapshot["dep_gate"] = "D1"
    prior_snapshot["arr_gate"] = "A1"
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T15:20:00Z",
        last_snapshot=prior_snapshot,
        phase_markers={
            "day_before_fired": True,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": False,
        },
    )
    write_flight_state(prior)
    write_active_flights([12345])

    # In-window (window opens 15:30 narrowbody). Dep gate D1→D6 AND arr gate A1→A2
    # in the same poll; the readout fires for the dep gate.
    fake_flight = _byair_flight(flight_id=12345, dep_gate="D6", dep_terminal="1")
    fake_flight["arrGate"] = "A2"
    fake_now = datetime(2026, 5, 18, 15, 40, 0, tzinfo=timezone.utc)

    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)

    gate_changes = [e["event"] for e in events if e["event"]["reason"] == "gate_change"]
    sides = {gc["side"] for gc in gate_changes}
    assert sides == {"arr"}, f"expected only the arr gate_change to survive, got {gate_changes}"
    assert any(e["event"]["reason"] == "gate_assignment" for e in events)


def test_gate_change_surfaces_when_readout_never_fired(state_root: Path):
    """#103 regression: a flight already in flight (the gate-readout never fires —
    the window is past and the flight is en_route) must still surface gate moves.
    Suppression is window-based, so an arrival-gate change mid-flight is NOT muted
    forever by gate_assignment_fired staying false."""
    enroute_snapshot = {
        "code": "XX123",
        "computed_status": "en_route",
        "computed_status_detail": "...",
        "computed_phase_progress": None,
        "computed_phase_risk": None,
        "computed_phase_overdue": None,
        "dep_gate": "B7",
        "arr_gate": "G1",
        "dep_terminal": None,
        "arr_terminal": None,
        "dep_time": "2026-05-18T17:00:00+00:00",
        "arr_time": "2026-05-18T20:30:00+00:00",
        "baggage": None,
        "inbound": {
            "aircraft_model": None,
            "registration": None,
            "flew": None,
            "predicted_delay_minutes": None,
        },
        "position_lat": None,
        "position_lon": None,
    }
    prior = _make_state(
        flight_id=12345,
        scheduled_arr_time="2026-05-18T20:30:00+00:00",
        last_polled_at="2026-05-18T19:50:00Z",
        last_snapshot=enroute_snapshot,
        phase_markers={
            "day_before_fired": True,
            "time_to_leave_fired": True,
            "boarding_fired": True,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": False,  # readout never fired (late-tracked)
        },
    )
    write_flight_state(prior)
    write_active_flights([12345])

    # Arrival gate moves G1 → G5 while en_route, well past the readout window.
    fake_flight = _byair_flight(flight_id=12345, computed_status="en_route", dep_gate="B7")
    fake_flight["arrGate"] = "G5"
    fake_flight["arrTime"] = "2026-05-18T20:30:00+00:00"
    fake_now = datetime(2026, 5, 18, 20, 0, 0, tzinfo=timezone.utc)  # mid-flight, window long past

    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)

    reasons = [e["event"]["reason"] for e in events]
    assert "gate_change" in reasons  # arrival-gate move surfaces, not muted forever
    assert "gate_assignment" not in reasons  # readout never fires for a departed flight


def test_poll_cycle_preserves_calendar_events_ledger(state_root: Path):
    """A poll rewrite must not wipe the reconcile-owned calendar_events ledger.

    The reconcile script (calendar_reconcile.py) writes calendar_events on the
    wake cycle; the precheck rewrites state on every poll. If the rewrite
    dropped the ledger, the boarding/flight event tracking — and the teardown
    tombstone it doubles as — would be lost every ~2 minutes. The ledger must
    survive verbatim through a poll that updates the snapshot.
    """
    ledger = {
        "boarding": {
            "event_id": "evt_boarding_1",
            "calendar_id": "c_byair@group.calendar.google.com",
            "managed": "created",
            "synced_signature": "2026-05-18T16:30:00+00:00/2026-05-18T17:00:00+00:00",
        },
        "flight": {
            "event_id": "evt_flight_1",
            "calendar_id": "c_byair@group.calendar.google.com",
            "managed": "adopted",
            "synced_signature": "2026-05-18T17:00:00+00:00/2026-05-18T20:00:00+00:00",
        },
    }
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T15:00:00Z",
        last_snapshot=_scheduled_snapshot(),
        calendar_events=ledger,
    )
    write_flight_state(prior)
    write_active_flights([12345])

    fake_flight = _byair_flight(flight_id=12345, dep_gate="B7")  # gate change forces a rewrite
    fake_now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)

    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        precheck._run_cycle(now_utc=fake_now)

    reloaded = read_flight_state(12345)
    assert reloaded is not None
    assert reloaded["calendar_events"] == ledger


def test_cadence_gating_skips_recent_polls(state_root: Path):
    """A flight polled 1 minute ago shouldn't be re-polled at the 10-min interval."""
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T16:29:00Z",  # 1 min ago
        last_snapshot={
            "code": "XX123",
            "computed_status": "scheduled",
            "computed_status_detail": "...",
            "computed_phase_progress": None,
            "computed_phase_risk": None,
            "computed_phase_overdue": None,
            "dep_gate": None,
            "arr_gate": None,
            "dep_terminal": None,
            "arr_terminal": None,
            "dep_time": "2026-05-18T17:00:00+00:00",
            "arr_time": None,
            "baggage": None,
            "inbound": {
                "aircraft_model": None,
                "registration": None,
                "flew": None,
                "predicted_delay_minutes": None,
            },
            "position_lat": None,
            "position_lon": None,
        },
    )
    write_flight_state(prior)
    write_active_flights([12345])

    fake_now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        events = precheck._run_cycle(now_utc=fake_now)
        # get_flight must NOT have been called (cadence-gated out)
        assert mock_byair_from_env.return_value.get_flight.call_count == 0
    assert events == []


def test_seeded_state_with_no_snapshot_forces_poll(state_root: Path):
    """sync_tripit seeds last_polled_at=now() + last_snapshot=None. The next
    precheck cycle MUST poll byAir despite the fresh last_polled_at, because
    last_snapshot is the de-facto 'byAir polled successfully' sentinel.

    Regression for jbaruch/nanoclaw-flight-assist#26.
    """
    # dep is 23h 30m after fake_now, so the T-24h day_before threshold has
    # already passed by 30 min — day_before must fire on this forced poll.
    scheduled_dep = "2026-05-19T16:00:00+00:00"
    scheduled_arr = "2026-05-19T19:00:00+00:00"
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T16:29:50Z",  # 10s ago — well within 30-min cadence
        scheduled_dep_time=scheduled_dep,
        scheduled_arr_time=scheduled_arr,
        last_snapshot=None,
    )
    write_flight_state(prior)
    write_active_flights([12345])

    fake_flight = _byair_flight(flight_id=12345, dep_time=scheduled_dep)
    fake_flight["scheduledDepTime"] = scheduled_dep
    fake_flight["scheduledArrTime"] = scheduled_arr
    fake_now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)
        assert mock_byair_from_env.return_value.get_flight.call_count == 1

    reasons = [e["event"]["reason"] for e in events]
    assert "day_before" in reasons
    persisted = read_flight_state(12345)
    assert persisted["last_snapshot"] is not None
    assert persisted["phase_markers"]["day_before_fired"] is True


def test_wall_clock_budget_defers_remaining_slow_flights(state_root: Path):
    """Many active flights on slow upstreams must not let the cumulative poll
    time exceed the agent-runner's 30s execFile hard-kill (#36).

    Each byAir poll here burns 9 simulated seconds via an injected monotonic
    clock; eight sequential polls would total 72s and trip the kill. The
    wall-clock budget must stop starting new polls partway through and defer
    the rest, leaving their state (and `last_polled_at`) untouched so the
    cadence gate retries them next cycle.
    """
    active_ids = [1, 2, 3, 4, 5, 6, 7, 8]
    write_active_flights(active_ids)

    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def slow_poll(*, flight_id: int) -> dict:
        clock["t"] += 9.0  # each poll burns 9 simulated seconds
        return _byair_flight(flight_id=flight_id)

    fake_now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.side_effect = slow_poll
        precheck._run_cycle(now_utc=fake_now, monotonic=fake_monotonic)

    polled_count = mock_byair_from_env.return_value.get_flight.call_count
    # The budget must engage: at least one flight polled, at least one deferred.
    assert 1 <= polled_count < len(active_ids)
    # Cumulative simulated poll time stayed under the 30s kill — without the
    # budget all eight polls would total 72s and the cycle would be killed.
    assert clock["t"] < precheck._SCRIPT_KILL_BUDGET_SECONDS
    # Polled flights (the first `polled_count` in index order) wrote state;
    # deferred flights were never touched, so they have no state on disk and
    # their cadence gate fires again next cycle.
    for fid in active_ids[:polled_count]:
        assert read_flight_state(fid) is not None
    for fid in active_ids[polled_count:]:
        assert read_flight_state(fid) is None


def test_poll_horizon_skips_flight_departing_beyond_24h(state_root: Path):
    """A seeded flight (no snapshot yet) departing more than 24h out is not
    polled — sync keeps it in the index, but it costs no byAir call until it
    approaches departure (#38). Its state is left untouched, so the cadence
    gate picks it up once it crosses into the horizon.
    """
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T16:00:00Z",
        scheduled_dep_time="2026-05-19T22:00:00+00:00",  # 30h after fake_now
        scheduled_arr_time="2026-05-20T01:00:00+00:00",
        last_snapshot=None,
    )
    write_flight_state(prior)
    write_active_flights([12345])

    fake_now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        events = precheck._run_cycle(now_utc=fake_now)
        # Beyond the horizon → no byAir poll at all.
        assert mock_byair_from_env.return_value.get_flight.call_count == 0
    assert events == []
    persisted = read_flight_state(12345)
    assert persisted["last_snapshot"] is None
    assert persisted["last_polled_at"] == "2026-05-18T16:00:00Z"


def test_poll_horizon_polls_flight_just_inside_24h(state_root: Path):
    """A seeded flight departing within the 24h horizon is polled normally
    (#38) — guards against an off-by-one that would starve in-window flights.
    """
    scheduled_dep = "2026-05-19T15:00:00+00:00"  # 23h after fake_now
    scheduled_arr = "2026-05-19T18:00:00+00:00"
    prior = _make_state(
        flight_id=12345,
        last_polled_at="2026-05-18T16:00:00Z",
        scheduled_dep_time=scheduled_dep,
        scheduled_arr_time=scheduled_arr,
        last_snapshot=None,
    )
    write_flight_state(prior)
    write_active_flights([12345])

    fake_flight = _byair_flight(flight_id=12345, dep_time=scheduled_dep)
    fake_flight["scheduledDepTime"] = scheduled_dep
    fake_flight["scheduledArrTime"] = scheduled_arr
    fake_now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        precheck._run_cycle(now_utc=fake_now)
        assert mock_byair_from_env.return_value.get_flight.call_count == 1
    persisted = read_flight_state(12345)
    assert persisted["last_snapshot"] is not None


# ---------------------------------------------------------------------------
# Script-level (subprocess) test for the JSON contract
# ---------------------------------------------------------------------------


def test_script_emits_single_line_json_with_no_active_flights(tmp_path: Path):
    """Run precheck.py as a subprocess; verify the last-line JSON contract."""
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    (state / "active-flights.json").write_text(json.dumps({"schema_version": 1, "flight_ids": []}))
    env = {
        "FLIGHT_ASSIST_STATE_DIR": str(state),
        "BYAIR_MCP_URL": "https://api.byairapp.example/mcp?api_key=test",
        "PATH": "/usr/bin:/bin",
    }
    script = REPO_ROOT / "skills" / "flight-assist" / "precheck.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    last_line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(last_line)
    assert payload == {"wake_agent": False, "data": {"events": []}}


def test_byair_transport_error_degrades_only_that_flight(state_root: Path):
    """A urllib URLError from byair_client should NOT collapse the entire cycle.

    Per `coding-policy: error-handling` "Specific Exceptions" + "Graceful Fallback".
    """
    write_active_flights([12345])
    fake_now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.side_effect = urllib.error.URLError(
            "synthetic network failure"
        )
        # _run_cycle must return cleanly (no exception); the URLError is
        # caught at the inner boundary so other flights would still poll.
        events = precheck._run_cycle(now_utc=fake_now)
    assert events == []
    # No state should have been written for this flight (no last_polled_at
    # update, so it retries next cycle)
    assert read_flight_state(12345) is None


def test_script_safe_shape_on_byair_misconfig(tmp_path: Path):
    """Missing BYAIR_MCP_URL — script should emit safe-shape JSON, exit 0."""
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    (state / "active-flights.json").write_text(
        json.dumps({"schema_version": 1, "flight_ids": [12345]})
    )
    env = {
        "FLIGHT_ASSIST_STATE_DIR": str(state),
        # BYAIR_MCP_URL deliberately unset
        "PATH": "/usr/bin:/bin",
    }
    script = REPO_ROOT / "skills" / "flight-assist" / "precheck.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # The outer-boundary contract: exit 0 (don't break the scheduler),
    # but emit safe-shape JSON with no events. Stderr carries the
    # traceback for debug.
    assert result.returncode == 0
    last_line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(last_line)
    assert payload["wake_agent"] is False
    assert "BYAIR_MCP_URL" in result.stderr or "precheck_exception" in last_line


# ---------------------------------------------------------------------------
# Connection-risk integration
# ---------------------------------------------------------------------------


def test_connection_risk_pass_fires_and_persists_marker(state_root: Path):
    """Two-leg trip with tight transfer: cycle should emit connection_at_risk
    and flip the leg-2 marker so the next cycle does not re-fire."""
    leg1 = _make_state(
        flight_id=1,
        last_polled_at="2026-05-18T16:29:30Z",
        trip_id=999,
        scheduled_dep_time="2026-05-18T17:00:00+00:00",
        scheduled_arr_time="2026-05-18T19:00:00+00:00",
        dep_airport_id=20,
        arr_airport_id=28,
        last_snapshot={
            "code": "AA100",
            "computed_status": "departed",
            "computed_status_detail": "...",
            "computed_phase_progress": None,
            "computed_phase_risk": None,
            "computed_phase_overdue": None,
            "dep_gate": None,
            "arr_gate": None,
            "dep_terminal": None,
            "arr_terminal": None,
            "dep_time": "2026-05-18T17:00:00+00:00",
            "arr_time": "2026-05-18T19:30:00+00:00",  # +30 min delay
            "baggage": None,
            "inbound": {
                "aircraft_model": None,
                "registration": None,
                "flew": None,
                "predicted_delay_minutes": None,
            },
            "position_lat": None,
            "position_lon": None,
        },
    )
    leg2 = _make_state(
        flight_id=2,
        last_polled_at="2026-05-18T16:29:30Z",
        trip_id=999,
        code="AA200",
        scheduled_dep_time="2026-05-18T20:00:00+00:00",  # 30 min from projected arr
        scheduled_arr_time="2026-05-18T22:00:00+00:00",
        dep_airport_id=28,
        arr_airport_id=40,
        last_snapshot=_scheduled_snapshot(code="AA200"),
    )
    write_flight_state(leg1)
    write_flight_state(leg2)
    write_active_flights([1, 2])

    fake_now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env"):
        # Both flights have last_polled_at within cadence — get_flight not called
        events = precheck._run_cycle(now_utc=fake_now)

    risk_events = [e for e in events if e["event"]["reason"] == "connection_at_risk"]
    assert len(risk_events) == 1
    assert risk_events[0]["flight_id"] == 2
    assert risk_events[0]["event"]["transfer_minutes_remaining"] == 30
    assert risk_events[0]["event"]["min_transfer_minutes"] == 45

    # Marker persisted: next cycle should not re-fire
    leg2_after = read_flight_state(2)
    assert leg2_after["phase_markers"]["connection_at_risk_fired"] is True

    with patch("precheck.ByAirClient.from_env"):
        events_second = precheck._run_cycle(now_utc=fake_now)
    second_risks = [e for e in events_second if e["event"]["reason"] == "connection_at_risk"]
    assert second_risks == []


def test_connection_risk_excludes_removed_upstream_flights(state_root: Path):
    """A flight that emits removed_upstream this cycle must not also fuel
    a connection_at_risk derived from its now-stale on-disk snapshot."""
    from byair_client import ByAirError

    leg1 = _make_state(
        flight_id=1,
        last_polled_at="2026-05-18T16:00:00Z",  # 30 min ago — past 5-min cadence
        trip_id=999,
        scheduled_dep_time="2026-05-18T17:00:00+00:00",
        scheduled_arr_time="2026-05-18T19:00:00+00:00",
        dep_airport_id=20,
        arr_airport_id=28,
        last_snapshot={
            "code": "AA100",
            "computed_status": "departed",
            "computed_status_detail": "...",
            "computed_phase_progress": None,
            "computed_phase_risk": None,
            "computed_phase_overdue": None,
            "dep_gate": None,
            "arr_gate": None,
            "dep_terminal": None,
            "arr_terminal": None,
            "dep_time": "2026-05-18T17:00:00+00:00",
            "arr_time": "2026-05-18T19:30:00+00:00",
            "baggage": None,
            "inbound": {
                "aircraft_model": None,
                "registration": None,
                "flew": None,
                "predicted_delay_minutes": None,
            },
            "position_lat": None,
            "position_lon": None,
        },
    )
    leg2 = _make_state(
        flight_id=2,
        last_polled_at="2026-05-18T16:00:00Z",  # 30 min ago — past cadence
        trip_id=999,
        code="AA200",
        scheduled_dep_time="2026-05-18T20:00:00+00:00",
        scheduled_arr_time="2026-05-18T22:00:00+00:00",
        dep_airport_id=28,
        arr_airport_id=40,
        last_snapshot=None,
    )
    write_flight_state(leg1)
    write_flight_state(leg2)
    write_active_flights([1, 2])

    fake_now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
    # Both flights' polls return not_found upstream — their stale state
    # must not feed the connection-risk pass even though on-disk it
    # would otherwise emit a tight-connection alert.
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.side_effect = ByAirError(
            "not_found", "Flight not found"
        )
        events = precheck._run_cycle(now_utc=fake_now)

    # removed_upstream fires for both flights (both got 404)
    removed_reasons = [e for e in events if e["event"]["reason"] == "removed_upstream"]
    assert {e["flight_id"] for e in removed_reasons} == {1, 2}

    # No connection_at_risk — both legs were removed upstream this cycle
    assert not any(e["event"]["reason"] == "connection_at_risk" for e in events)


def test_connection_risk_excludes_poll_failed_flights(state_root: Path):
    """A flight whose poll attempted but failed (transport error) this cycle
    is unverified and must not contribute to a derived connection_at_risk."""
    leg1 = _make_state(
        flight_id=1,
        last_polled_at="2026-05-18T16:00:00Z",  # past 5-min cadence
        trip_id=999,
        scheduled_dep_time="2026-05-18T17:00:00+00:00",
        scheduled_arr_time="2026-05-18T19:00:00+00:00",
        dep_airport_id=20,
        arr_airport_id=28,
        last_snapshot={
            "code": "AA100",
            "computed_status": "departed",
            "computed_status_detail": "...",
            "computed_phase_progress": None,
            "computed_phase_risk": None,
            "computed_phase_overdue": None,
            "dep_gate": None,
            "arr_gate": None,
            "dep_terminal": None,
            "arr_terminal": None,
            "dep_time": "2026-05-18T17:00:00+00:00",
            "arr_time": "2026-05-18T19:30:00+00:00",
            "baggage": None,
            "inbound": {
                "aircraft_model": None,
                "registration": None,
                "flew": None,
                "predicted_delay_minutes": None,
            },
            "position_lat": None,
            "position_lon": None,
        },
    )
    leg2 = _make_state(
        flight_id=2,
        last_polled_at="2026-05-18T16:00:00Z",
        trip_id=999,
        code="AA200",
        scheduled_dep_time="2026-05-18T20:00:00+00:00",
        scheduled_arr_time="2026-05-18T22:00:00+00:00",
        dep_airport_id=28,
        arr_airport_id=40,
        last_snapshot=None,
    )
    write_flight_state(leg1)
    write_flight_state(leg2)
    write_active_flights([1, 2])

    fake_now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
    # Both polls fail with URLError — neither was verified this cycle.
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.side_effect = urllib.error.URLError(
            "synthetic network failure"
        )
        events = precheck._run_cycle(now_utc=fake_now)

    # No removed_upstream (the failures weren't 404s) and no
    # connection_at_risk (snapshots unverified this cycle).
    assert not any(e["event"]["reason"] == "connection_at_risk" for e in events)
    assert not any(e["event"]["reason"] == "removed_upstream" for e in events)


def test_connection_risk_honors_config_override(state_root: Path):
    """config.json's min_transfer_minutes overrides the default."""
    from state import write_config

    write_config({"min_transfer_minutes": 20})

    leg1 = _make_state(
        flight_id=1,
        last_polled_at="2026-05-18T16:29:30Z",
        trip_id=999,
        scheduled_dep_time="2026-05-18T17:00:00+00:00",
        scheduled_arr_time="2026-05-18T19:00:00+00:00",
        dep_airport_id=20,
        arr_airport_id=28,
        last_snapshot={
            "code": "AA100",
            "computed_status": "departed",
            "computed_status_detail": "...",
            "computed_phase_progress": None,
            "computed_phase_risk": None,
            "computed_phase_overdue": None,
            "dep_gate": None,
            "arr_gate": None,
            "dep_terminal": None,
            "arr_terminal": None,
            "dep_time": "2026-05-18T17:00:00+00:00",
            "arr_time": "2026-05-18T19:30:00+00:00",
            "baggage": None,
            "inbound": {
                "aircraft_model": None,
                "registration": None,
                "flew": None,
                "predicted_delay_minutes": None,
            },
            "position_lat": None,
            "position_lon": None,
        },
    )
    leg2 = _make_state(
        flight_id=2,
        last_polled_at="2026-05-18T16:29:30Z",
        trip_id=999,
        code="AA200",
        scheduled_dep_time="2026-05-18T20:00:00+00:00",  # 30 min — above 20 override
        scheduled_arr_time="2026-05-18T22:00:00+00:00",
        dep_airport_id=28,
        arr_airport_id=40,
        last_snapshot=_scheduled_snapshot(code="AA200"),
    )
    write_flight_state(leg1)
    write_flight_state(leg2)
    write_active_flights([1, 2])

    fake_now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env"):
        events = precheck._run_cycle(now_utc=fake_now)
    assert not any(e["event"]["reason"] == "connection_at_risk" for e in events)


def test_connection_risk_excludes_budget_deferred_flights(state_root: Path):
    """A flight deferred because the wall-clock budget elapsed is unverified
    this cycle and must not feed a derived connection_at_risk — same exclusion
    as a poll-failed flight (#36).

    A decoy first-cycle flight (id 9) burns the entire budget on its poll, so
    both legs of the trip-999 transfer are deferred before they're reached.
    Their tight-transfer on-disk state is identical to the fixture in
    `test_connection_risk_pass_fires_and_persists_marker` (which fires when the
    legs are eligible), so the only reason no risk fires here is the deferral.
    The legs' state — including `last_polled_at` and the not-yet-fired marker —
    must be left untouched for the next cycle.
    """
    leg1 = _make_state(
        flight_id=1,
        last_polled_at="2026-05-18T16:29:30Z",
        trip_id=999,
        scheduled_dep_time="2026-05-18T17:00:00+00:00",
        scheduled_arr_time="2026-05-18T19:00:00+00:00",
        dep_airport_id=20,
        arr_airport_id=28,
        last_snapshot={
            "code": "AA100",
            "computed_status": "departed",
            "computed_status_detail": "...",
            "computed_phase_progress": None,
            "computed_phase_risk": None,
            "computed_phase_overdue": None,
            "dep_gate": None,
            "arr_gate": None,
            "dep_terminal": None,
            "arr_terminal": None,
            "dep_time": "2026-05-18T17:00:00+00:00",
            "arr_time": "2026-05-18T19:30:00+00:00",  # +30 min delay
            "baggage": None,
            "inbound": {
                "aircraft_model": None,
                "registration": None,
                "flew": None,
                "predicted_delay_minutes": None,
            },
            "position_lat": None,
            "position_lon": None,
        },
    )
    leg2 = _make_state(
        flight_id=2,
        last_polled_at="2026-05-18T16:29:30Z",
        trip_id=999,
        code="AA200",
        scheduled_dep_time="2026-05-18T20:00:00+00:00",  # 30 min from projected arr
        scheduled_arr_time="2026-05-18T22:00:00+00:00",
        dep_airport_id=28,
        arr_airport_id=40,
        last_snapshot=_scheduled_snapshot(code="AA200"),
    )
    write_flight_state(leg1)
    write_flight_state(leg2)
    # Decoy (id 9) is first in the index and first-cycle (no prior state), so
    # it polls and exhausts the budget before either leg is reached.
    write_active_flights([9, 1, 2])

    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        return clock["t"]

    def slow_poll(*, flight_id: int) -> dict:
        clock["t"] += 25.0  # a single poll exhausts the wall-clock budget
        return _byair_flight(flight_id=flight_id)

    fake_now = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.side_effect = slow_poll
        events = precheck._run_cycle(now_utc=fake_now, monotonic=fake_monotonic)

    # Only the decoy was polled; both trip-999 legs hit the budget and deferred.
    assert mock_byair_from_env.return_value.get_flight.call_count == 1
    # Deferred legs are excluded from the risk pass → no connection_at_risk.
    assert not any(e["event"]["reason"] == "connection_at_risk" for e in events)
    # Both legs untouched: last_polled_at unchanged, marker not flipped.
    for fid in (1, 2):
        after = read_flight_state(fid)
        assert after["last_polled_at"] == "2026-05-18T16:29:30Z"
        assert after["phase_markers"]["connection_at_risk_fired"] is False


def test_connection_risk_fires_when_leg2_is_beyond_poll_horizon(state_root: Path):
    """The 24h poll horizon (#38) must NOT suppress a connection_at_risk for a
    leg-2 that sits just past the horizon while leg-1 is imminent.

    leg-1 departs within 24h (polled), leg-2 departs ~26.5h out (horizon-
    skipped, never polled, last_snapshot stays None). The transfer is tight
    (30 min < 45). detect_connection_risks reads only leg-2's seeded
    scheduled_dep_time / dep_airport_id / marker — none of which polling
    refreshes — and gates leg-1 on its own 24h lookahead using leg-1's live
    snapshot, so the alert still fires. This guards against a regression that
    would exclude horizon-skipped flights from the cross-flight pass.
    """
    leg1 = _make_state(
        flight_id=1,
        last_polled_at="2026-05-18T15:00:00Z",
        trip_id=999,
        scheduled_dep_time="2026-05-19T15:00:00+00:00",  # 23h out — inside horizon
        scheduled_arr_time="2026-05-19T18:00:00+00:00",
        dep_airport_id=20,
        arr_airport_id=28,
        last_snapshot=None,  # seeded; forces a poll this cycle
    )
    leg2 = _make_state(
        flight_id=2,
        last_polled_at="2026-05-18T15:00:00Z",
        trip_id=999,
        code="AA200",
        scheduled_dep_time="2026-05-19T18:30:00+00:00",  # 26.5h out — beyond horizon
        scheduled_arr_time="2026-05-19T21:00:00+00:00",
        dep_airport_id=28,
        arr_airport_id=40,
        last_snapshot=None,
    )
    write_flight_state(leg1)
    write_flight_state(leg2)
    write_active_flights([1, 2])

    fake_flight = _byair_flight(flight_id=1)
    fake_flight["scheduledDepTime"] = "2026-05-19T15:00:00+00:00"
    fake_flight["scheduledArrTime"] = "2026-05-19T18:00:00+00:00"
    fake_now = datetime(2026, 5, 18, 16, 0, 0, tzinfo=timezone.utc)
    with patch("precheck.ByAirClient.from_env") as mock_byair_from_env:
        mock_byair_from_env.return_value.get_flight.return_value = fake_flight
        events = precheck._run_cycle(now_utc=fake_now)
        # leg-1 polled, leg-2 horizon-skipped → exactly one byAir call.
        assert mock_byair_from_env.return_value.get_flight.call_count == 1

    risk_events = [e for e in events if e["event"]["reason"] == "connection_at_risk"]
    assert len(risk_events) == 1
    assert risk_events[0]["flight_id"] == 2
    assert risk_events[0]["event"]["transfer_minutes_remaining"] == 30
    # leg-2 marker flipped by the risk pass, but it was never polled.
    leg2_after = read_flight_state(2)
    assert leg2_after["phase_markers"]["connection_at_risk_fired"] is True
    assert leg2_after["last_snapshot"] is None
