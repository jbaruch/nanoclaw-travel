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
        "depTerminal": None,
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


def test_gate_change_event_propagates_through_cycle(state_root: Path):
    """A gate change between prior state and new fetch produces a gate_change event."""
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
    scheduled_dep = "2026-05-19T16:00:00+00:00"  # T-24h relative to fake_now
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
