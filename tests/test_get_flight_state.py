"""Tests for skills/flight-assist/scripts/get-flight-state.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "skills" / "flight-assist" / "scripts" / "get-flight-state.py"


def _run(args: list[str], *, state_dir: Path) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env={"FLIGHT_ASSIST_STATE_DIR": str(state_dir), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    return (result.returncode, result.stdout, result.stderr)


def _seed_flight_state(state_dir: Path, flight_id: int) -> dict:
    """Drop a valid flight-state JSON into state_dir."""
    state_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
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
        },
        "last_wake_at": None,
        "last_wake_reason": None,
    }
    (state_dir / f"flight-{flight_id}.json").write_text(json.dumps(record))
    return record


def test_reads_existing_flight_state(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    expected = _seed_flight_state(state, 12345)
    code, stdout, _ = _run(["12345"], state_dir=state)
    assert code == 0
    payload = json.loads(stdout.strip())
    assert payload["flight_id"] == 12345
    assert payload["code"] == expected["code"]


def test_returns_error_for_missing_flight(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    code, stdout, _ = _run(["99999"], state_dir=state)
    assert code == 0
    payload = json.loads(stdout.strip())
    assert "error" in payload
    assert "99999" in payload["error"]


def test_rejects_non_integer_flight_id(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    code, _, stderr = _run(["not-a-number"], state_dir=state)
    assert code == 2
    payload = json.loads(stderr.strip())
    assert "flight_id" in payload["error"]


def test_missing_argument_exits_2(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    code, _, stderr = _run([], state_dir=state)
    assert code == 2
    payload = json.loads(stderr.strip())
    assert "usage" in payload["error"]
