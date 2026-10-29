"""The existing configuration owner retains/validates additive EV preferences."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/flight-assist"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/drive-engine"))
from ev_preferences import validate_ev_preferences  # noqa: E402
from state import STATE_SCHEMA_VERSION, read_config, state_dir, write_config  # noqa: E402


def preferences():
    return {
        "schema_version": 1,
        "stop_minutes": 30,
        "reserve_percent": 10,
        "cold_allowance_percent": 20,
        "highway_allowance_percent": 10,
        "default_vehicle": None,
        "event_vehicles": {"event": "Car"},
        "person_vehicles": {"Driver": "Other"},
        "event_drivers": {},
        "destination_charges": [
            {
                "schema_version": 1,
                "vehicle": "Car",
                "destination": "Venue",
                "available_at": "2020-07-13T20:00:00Z",
                "energy_kwh": 40,
                "source": "Owner confirmed venue charging, message native-42",
            }
        ],
    }


def test_existing_config_owner_round_trip_and_additive_v7_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    path = state_dir() / "config.json"
    path.write_text(json.dumps({"schema_version": 7, "home_address": "Home"}))
    assert read_config() == {"schema_version": STATE_SCHEMA_VERSION, "home_address": "Home"}
    assert json.loads(path.read_text())["schema_version"] == STATE_SCHEMA_VERSION
    write_config({"home_address": "Home", "ev_planning": preferences()})
    current = read_config()
    assert current is not None and current["ev_planning"] == preferences()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"stop_minutes": True},
        {"stop_minutes": 0},
        {"reserve_percent": 51},
        {"reserve_percent": 10.5},
        {"highway_allowance_percent": float("nan")},
        {"cold_allowance_percent": -1},
        {"default_vehicle": " "},
        {"default_vehicle": "Car\nOther"},
        {"event_vehicles": {"event": 2}},
        {"event_drivers": []},
        {"person_vehicles": {"Driver": ""}},
        {"extra": True},
    ],
)
def test_invalid_preferences_preserve_existing_config(tmp_path, monkeypatch, changes):
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    write_config({"home_address": "Home"})
    path = state_dir() / "config.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_config({"ev_planning": {**preferences(), **changes}})
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"source": ""},
        {"energy_kwh": float("inf")},
        {"energy_kwh": -1},
        {"vehicle": ""},
        {"destination": ""},
        {"schema_version": 2},
        {"available_at": "2020-07-13T20:00:00"},
    ],
)
def test_destination_charge_requires_complete_grounding(changes):
    value = preferences()
    value["destination_charges"][0].update(changes)
    with pytest.raises(ValueError):
        validate_ev_preferences(value)


def test_same_charge_cannot_be_counted_twice():
    value = preferences()
    value["destination_charges"].append(copy.deepcopy(value["destination_charges"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        validate_ev_preferences(value)


def test_shared_reader_preserves_unknown_preferences_without_interpreting_them(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    value = preferences()
    value["schema_version"] = 99
    path = state_dir() / "config.json"
    path.write_text(json.dumps({"schema_version": STATE_SCHEMA_VERSION, "ev_planning": value}))
    before = path.read_bytes()
    result = read_config()
    assert result is not None and result["ev_planning"] == value
    assert path.read_bytes() == before


def test_root_migration_preserves_unknown_nested_ev_fields_for_their_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    path = state_dir() / "config.json"
    path.write_text('{"schema_version":7,"ev_planning":{"schema_version":99}}')
    assert read_config() == {
        "schema_version": STATE_SCHEMA_VERSION,
        "ev_planning": {"schema_version": 99},
    }


def test_intake_preserves_other_config_and_calls_no_provider(tmp_path, monkeypatch, capsys):
    import stat

    import configure_ev

    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    write_config({"home_address": "Home", "min_transfer_minutes": 90})
    assert configure_ev.main([json.dumps(preferences())]) == 0
    assert json.loads(capsys.readouterr().out) == {"schema_version": 1, "recorded": True}
    config = read_config()
    assert config is not None
    assert config["home_address"] == "Home" and config["min_transfer_minutes"] == 90
    assert config["ev_planning"] == preferences()
    assert stat.S_IMODE((state_dir() / "config.json").stat().st_mode) == 0o600


def test_intake_duplicate_json_does_not_change_config(tmp_path, monkeypatch, capsys):
    import configure_ev

    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    write_config({"home_address": "Home"})
    path = state_dir() / "config.json"
    before = path.read_bytes()
    assert configure_ev.main(['{"schema_version":1,"schema_version":99}']) == 1
    assert path.read_bytes() == before
    assert json.loads(capsys.readouterr().out)["recorded"] is False


def test_intake_migration_stays_private_if_later_preference_write_fails(tmp_path, monkeypatch):
    import os
    import stat

    import configure_ev

    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    path = state_dir() / "config.json"
    path.write_text('{"schema_version":7,"home_address":"Home"}')
    path.chmod(0o600)

    def unavailable(_value):
        raise OSError("synthetic preference write failure")

    monkeypatch.setattr(configure_ev, "write_config", unavailable)
    previous = os.umask(0o022)
    try:
        assert configure_ev.main([json.dumps(preferences())]) == 1
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["home_address"] == "Home"


def test_intake_usage_error_has_no_json_result_or_config_write(tmp_path, monkeypatch, capsys):
    import configure_ev

    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    assert configure_ev.main([]) == 2
    result = capsys.readouterr()
    assert result.out == "" and "Supply one" in result.err
    assert not (state_dir() / "config.json").exists()


def test_intake_still_refuses_unknown_existing_ev_schema_without_downgrade(tmp_path, monkeypatch):
    import configure_ev

    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    path = state_dir() / "config.json"
    path.write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION, "ev_planning": {"schema_version": 99}})
    )
    before = path.read_bytes()
    assert configure_ev.main([json.dumps(preferences())]) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [None, [], "unknown", True])
def test_opaque_preservation_cannot_introduce_a_new_invalid_ev_field(tmp_path, monkeypatch, value):
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    write_config({"home_address": "Home"})
    path = state_dir() / "config.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_config({"home_address": "Home", "ev_planning": value})
    assert path.read_bytes() == before


def test_opaque_preservation_rejects_type_changed_invalid_record(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    write_config({"home_address": "Home"})
    path = state_dir() / "config.json"
    stored = json.loads(path.read_text())
    stored["ev_planning"] = {"schema_version": 99, "future": True}
    path.write_text(json.dumps(stored))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_config(
            {"home_address": "Changed", "ev_planning": {"schema_version": 99, "future": 1}}
        )
    assert path.read_bytes() == before


def test_private_owner_failed_atomic_replacement_preserves_file_and_foreign_temp(
    tmp_path, monkeypatch
):
    import os
    import stat

    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    write_config({"home_address": "Old", "ev_planning": preferences()})
    path = state_dir() / "config.json"
    before = path.read_bytes()
    foreign = state_dir() / "config.json.tmp"
    foreign.write_text("unrelated retained artifact")

    def unavailable(*_args):
        raise OSError("synthetic rename refusal")

    monkeypatch.setattr(os, "replace", unavailable)
    with pytest.raises(OSError, match="synthetic"):
        write_config({"home_address": "New", "ev_planning": preferences()})
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert foreign.read_text() == "unrelated retained artifact"
    assert list(tmp_path.glob("config.json.*.tmp")) == []


def test_deep_intake_json_has_fixed_process_failure_without_state_change(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    write_config({"home_address": "Retained"})
    path = state_dir() / "config.json"
    before = path.read_bytes()
    script = Path(__file__).resolve().parents[1] / "skills/drive-engine/configure_ev.py"
    result = subprocess.run(
        [sys.executable, str(script), "[" * 2000 + "0" + "]" * 2000],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"schema_version": 1, "recorded": False}
    assert "Traceback" not in result.stderr and "preserve the existing file" in result.stderr
    assert path.read_bytes() == before
