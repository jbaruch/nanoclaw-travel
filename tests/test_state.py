"""Tests for skills/flight-assist/state.py.

Use the FLIGHT_ASSIST_STATE_DIR env var to redirect state to tmp_path,
keeping every test independent and self-cleaning per `coding-policy:
testing-standards` "Independence".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from state import (  # noqa: E402
    ACTIVE_FLIGHTS_FILE,
    CONFIG_FILE,
    STATE_SCHEMA_VERSION,
    StateError,
    delete_flight_state,
    read_active_flights,
    read_config,
    read_flight_state,
    state_dir,
    write_active_flights,
    write_config,
    write_flight_state,
)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    """Redirect FLIGHT_ASSIST_STATE_DIR to a per-test tmp dir."""
    root = tmp_path / "state" / "flight-assist"
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(root))
    return root


def test_state_dir_defaults_to_workspace(monkeypatch):
    monkeypatch.delenv("FLIGHT_ASSIST_STATE_DIR", raising=False)
    assert str(state_dir()) == "/workspace/state/flight-assist"


def test_state_dir_overrides_via_env_var(state_root: Path):
    assert state_dir() == state_root


def test_read_config_returns_none_when_missing(state_root: Path):
    assert read_config() is None


def test_write_then_read_config_roundtrips(state_root: Path):
    write_config({"home_address": "1 Fixture Loop, Cupertino, CA"})
    loaded = read_config()
    assert loaded is not None
    assert loaded["home_address"] == "1 Fixture Loop, Cupertino, CA"
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_write_config_overrides_caller_supplied_schema_version(state_root: Path):
    """Caller-supplied schema_version is overridden by the canonical constant."""
    write_config({"home_address": "X", "schema_version": 99})
    loaded = read_config()
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_read_active_flights_returns_empty_when_missing(state_root: Path):
    assert read_active_flights() == []


def test_write_then_read_active_flights_roundtrips(state_root: Path):
    write_active_flights([100, 200, 300])
    assert read_active_flights() == [100, 200, 300]


def test_write_active_flights_overwrites(state_root: Path):
    write_active_flights([1, 2])
    write_active_flights([3, 4, 5])
    assert read_active_flights() == [3, 4, 5]


def test_read_flight_state_returns_none_when_missing(state_root: Path):
    assert read_flight_state(12345) is None


def test_write_then_read_flight_state_roundtrips(state_root: Path):
    state = {
        "flight_id": 12345,
        "code": "XX123",
        "ownership": "mine",
        "trip_id": 678,
        "scheduled_dep_time": "2026-05-17T09:00:00-07:00",
        "scheduled_arr_time": "2026-05-17T11:09:00-07:00",
        "dep_airport_id": 20,
        "arr_airport_id": 28,
        "last_polled_at": "2026-05-17T18:42:11Z",
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
    write_flight_state(state)
    loaded = read_flight_state(12345)
    assert loaded is not None
    assert loaded["flight_id"] == 12345
    assert loaded["code"] == "XX123"
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_write_flight_state_requires_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id"):
        write_flight_state({"code": "XX123"})


def test_write_flight_state_requires_integer_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id"):
        write_flight_state({"flight_id": "not-an-int", "code": "XX123"})


def test_delete_flight_state_removes_existing_file(state_root: Path):
    write_flight_state({"flight_id": 999})
    assert read_flight_state(999) is not None
    assert delete_flight_state(999) is True
    assert read_flight_state(999) is None


def test_delete_flight_state_returns_false_when_missing(state_root: Path):
    assert delete_flight_state(404) is False


def test_corrupt_json_raises_state_error(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text("{not valid json")
    with pytest.raises(StateError, match="not valid JSON"):
        read_config()


def test_missing_schema_version_raises_state_error(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(json.dumps({"home_address": "X"}))
    with pytest.raises(StateError, match="schema_version"):
        read_config()


def test_future_schema_version_raises_state_error(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(json.dumps({"schema_version": STATE_SCHEMA_VERSION + 99}))
    with pytest.raises(StateError, match="schema_version"):
        read_config()


def test_past_schema_version_raises_state_error(state_root: Path):
    """schema_version < current must raise StateError today (no migrations registered)."""
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(json.dumps({"schema_version": 0, "home_address": "X"}))
    with pytest.raises(StateError, match="schema_version"):
        read_config()


def test_string_schema_version_raises_state_error(state_root: Path):
    """A non-int schema_version (string) must raise StateError, not TypeError."""
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(json.dumps({"schema_version": "1"}))
    with pytest.raises(StateError, match="schema_version of type str"):
        read_config()


def test_bool_schema_version_raises_state_error(state_root: Path):
    """`bool` is a subclass of `int` in Python — exclude it explicitly."""
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(json.dumps({"schema_version": True}))
    with pytest.raises(StateError, match="schema_version of type bool"):
        read_config()


def test_float_schema_version_raises_state_error(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(json.dumps({"schema_version": 1.0}))
    with pytest.raises(StateError, match="schema_version of type float"):
        read_config()


def test_active_flights_missing_flight_ids_field_raises(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION})
    )
    with pytest.raises(StateError, match="missing required field"):
        read_active_flights()


def test_active_flights_with_string_element_raises(state_root: Path):
    """No silent coercion of stringified ints — strict type enforcement."""
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION, "flight_ids": [1, "2", 3]})
    )
    with pytest.raises(StateError, match=r"flight_ids\[1\] is str"):
        read_active_flights()


def test_active_flights_with_float_element_raises(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION, "flight_ids": [1, 2.5, 3]})
    )
    with pytest.raises(StateError, match=r"flight_ids\[1\] is float"):
        read_active_flights()


def test_active_flights_with_bool_element_raises(state_root: Path):
    """`True` would pass `isinstance(_, int)` without the bool guard."""
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION, "flight_ids": [True, 2]})
    )
    with pytest.raises(StateError, match=r"flight_ids\[0\] is bool"):
        read_active_flights()


def test_non_object_json_raises_state_error(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(json.dumps([1, 2, 3]))
    with pytest.raises(StateError, match="not a JSON object"):
        read_active_flights()


def test_active_flights_with_non_list_raises_state_error(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION, "flight_ids": "not-a-list"})
    )
    with pytest.raises(StateError, match="flight_ids"):
        read_active_flights()


def test_atomic_write_via_tmp_then_rename(state_root: Path):
    """A write must not leave a tmp file behind on success."""
    write_active_flights([1, 2, 3])
    leftover = list(state_root.glob("*.tmp"))
    assert leftover == []


def test_write_creates_state_directory(state_root: Path):
    """First write creates the state directory if missing."""
    assert not state_root.exists()
    write_active_flights([1])
    assert state_root.exists()


def test_state_files_use_separate_paths(state_root: Path):
    """Each flight gets its own file; writes to one don't disturb another."""
    write_flight_state({"flight_id": 100, "code": "A"})
    write_flight_state({"flight_id": 200, "code": "B"})
    assert read_flight_state(100)["code"] == "A"
    assert read_flight_state(200)["code"] == "B"
    files = sorted(p.name for p in state_root.iterdir())
    assert files == ["flight-100.json", "flight-200.json"]
