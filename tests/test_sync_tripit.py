"""Tests for skills/flight-assist/sync_tripit.py."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

import sync_tripit  # noqa: E402
from state import (  # noqa: E402
    read_active_flights,
    read_flight_state,
    write_active_flights,
)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "state" / "flight-assist"
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(root))
    monkeypatch.setenv("BYAIR_MCP_URL", "https://api.byairapp.example/mcp?api_key=test")
    return root


def _trips_payload(flights: list[dict]) -> dict:
    """Wrap a list of flight dicts in a single-trip byair_list_trips response."""
    return {
        "trips": [
            {
                "id": 678,
                "name": "Test Trip",
                "flights": flights,
            }
        ]
    }


def _flight(flight_id: int, code: str = "XX123") -> dict:
    return {
        "id": flight_id,
        "code": code,
        "ownership": "mine",
        "scheduledDepTime": "2026-05-18T17:00:00+00:00",
        "scheduledArrTime": "2026-05-18T20:00:00+00:00",
        "depAirport": {"id": 20, "name": "San Francisco International Airport"},
        "arrAirport": {"id": 28, "name": "Phoenix"},
    }


# ---------------------------------------------------------------------------
# Reconcile semantics
# ---------------------------------------------------------------------------


def test_first_sync_adds_all_upstream_flights(state_root: Path):
    payload = _trips_payload([_flight(100), _flight(200)])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert diff["added"] == [100, 200]
    assert diff["removed"] == []
    assert read_active_flights() == [100, 200]
    assert read_flight_state(100) is not None
    assert read_flight_state(200) is not None


def test_sync_with_unchanged_upstream_no_diff(state_root: Path):
    write_active_flights([100])
    sync_tripit.initialize_flight_from_byair(
        flight=_flight(100), now_utc=datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    )
    payload = _trips_payload([_flight(100)])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=datetime(2026, 5, 18, 4, 5, 0, tzinfo=timezone.utc))
    assert diff["added"] == []
    assert diff["removed"] == []


def test_sync_removes_expired_flights(state_root: Path):
    # Initial state: tracking 100 and 200
    write_active_flights([100, 200])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    sync_tripit.initialize_flight_from_byair(flight=_flight(100), now_utc=fake_now)
    sync_tripit.initialize_flight_from_byair(flight=_flight(200), now_utc=fake_now)
    # Upstream now only reports 100
    payload = _trips_payload([_flight(100)])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert diff["added"] == []
    assert diff["removed"] == [200]
    assert read_active_flights() == [100]
    assert read_flight_state(200) is None
    assert read_flight_state(100) is not None


def test_sync_adds_and_removes_in_one_pass(state_root: Path):
    write_active_flights([100])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    sync_tripit.initialize_flight_from_byair(flight=_flight(100), now_utc=fake_now)
    payload = _trips_payload([_flight(200), _flight(300)])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert diff["added"] == [200, 300]
    assert diff["removed"] == [100]
    assert read_active_flights() == [200, 300]


def test_sync_handles_empty_upstream(state_root: Path):
    write_active_flights([100, 200])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    sync_tripit.initialize_flight_from_byair(flight=_flight(100), now_utc=fake_now)
    sync_tripit.initialize_flight_from_byair(flight=_flight(200), now_utc=fake_now)
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = {"trips": []}
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert diff["removed"] == [100, 200]
    assert read_active_flights() == []


def test_initialize_flight_with_flight_id_key(state_root: Path):
    """initialize_flight_from_byair tolerates `flight_id` instead of `id`."""
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    flight = {
        "flight_id": 42,  # precheck's internal key, not byair's `id`
        "code": "XX42",
        "ownership": "mine",
        "scheduledDepTime": "2026-05-18T17:00:00+00:00",
        "scheduledArrTime": "2026-05-18T20:00:00+00:00",
        "depAirport": {"id": 20},
        "arrAirport": {"id": 28},
    }
    sync_tripit.initialize_flight_from_byair(flight=flight, now_utc=fake_now)
    state = read_flight_state(42)
    assert state is not None
    assert state["flight_id"] == 42
    assert state["code"] == "XX42"


def test_initialize_flight_with_id_key(state_root: Path):
    """initialize_flight_from_byair also tolerates `id` (byair raw shape)."""
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    sync_tripit.initialize_flight_from_byair(flight=_flight(42), now_utc=fake_now)
    state = read_flight_state(42)
    assert state is not None
    assert state["flight_id"] == 42


def test_initialize_flight_with_no_id_is_a_no_op(state_root: Path):
    """A flight dict without either id or flight_id silently no-ops."""
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    sync_tripit.initialize_flight_from_byair(flight={"code": "XX"}, now_utc=fake_now)
    assert read_active_flights() == []  # nothing written


def test_sync_handles_multi_trip_payload(state_root: Path):
    """Each trip's flights all roll into the same active-flights index."""
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=timezone.utc)
    payload = {
        "trips": [
            {"id": 1, "name": "T1", "flights": [_flight(100)]},
            {"id": 2, "name": "T2", "flights": [_flight(200), _flight(300)]},
        ]
    }
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert diff["added"] == [100, 200, 300]
    assert read_flight_state(100)["trip_id"] == 1
    assert read_flight_state(200)["trip_id"] == 2
    assert read_flight_state(300)["trip_id"] == 2


# ---------------------------------------------------------------------------
# Script-level subprocess test
# ---------------------------------------------------------------------------


def test_script_emits_safe_shape_when_byair_unset(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    env = {
        "FLIGHT_ASSIST_STATE_DIR": str(state),
        "PATH": "/usr/bin:/bin",
        # BYAIR_MCP_URL deliberately unset
    }
    script = REPO_ROOT / "skills" / "flight-assist" / "sync_tripit.py"
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
    assert payload["wake_agent"] is False
    # Error context is in stderr; stdout stays a safe shape
    assert payload["data"].get("error") == "sync_exception"
