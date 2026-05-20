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
    CURRENT_LOCATION_FILE,
    STATE_SCHEMA_VERSION,
    StateError,
    delete_flight_state,
    read_active_flights,
    read_config,
    read_current_location,
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


def _make_flight_state(flight_id: int = 12345, **overrides) -> dict:
    """Build a minimum-valid flight-state dict for write_flight_state.

    Returns every required field per `state-schema.md`. Tests that want
    to exercise a specific field override it via `**overrides`.
    """
    base = {
        "flight_id": flight_id,
        "code": "XX123",
        "ownership": "mine",
        "trip_id": 678,
        "scheduled_dep_time": "2026-05-17T09:00:00-07:00",
        "scheduled_arr_time": "2026-05-17T11:09:00-07:00",
        "dep_airport_id": 20,
        "arr_airport_id": 28,
        "last_polled_at": "2026-05-17T18:42:11Z",
        "phase_markers": {
            "day_before_fired": False,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
        },
        "last_snapshot": None,
        "last_wake_at": None,
        "last_wake_reason": None,
    }
    base.update(overrides)
    return base


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


def test_read_current_location_returns_none_when_missing(state_root: Path):
    """No `current-location.json` on disk → reader returns None
    (caller falls back to `home_address`)."""
    assert read_current_location() is None


def test_read_current_location_roundtrips_valid_payload(state_root: Path):
    """Well-formed snapshot is returned with the documented fields."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / CURRENT_LOCATION_FILE).write_text(
        json.dumps(
            {
                "latitude": 59.6519,
                "longitude": 17.9186,
                "captured_at": "2026-05-20T11:42:11Z",
            }
        )
    )
    loc = read_current_location()
    assert loc == {
        "latitude": 59.6519,
        "longitude": 17.9186,
        "captured_at": "2026-05-20T11:42:11Z",
    }


@pytest.mark.parametrize(
    "payload",
    [
        "not even json {{{",
        json.dumps([1, 2, 3]),
        json.dumps({"latitude": "59.6519", "longitude": 17.9186, "captured_at": "2026"}),
        json.dumps({"latitude": True, "longitude": 17.9186, "captured_at": "2026"}),
        json.dumps({"latitude": 59.6519, "longitude": 17.9186}),
        json.dumps({"latitude": 999.0, "longitude": 0.0, "captured_at": "2026"}),
        json.dumps({"latitude": 0.0, "longitude": 999.0, "captured_at": "2026"}),
    ],
    ids=[
        "malformed-json",
        "list-payload",
        "lat-as-string",
        "lat-as-bool",
        "missing-captured_at",
        "lat-out-of-range",
        "lng-out-of-range",
    ],
)
def test_read_current_location_returns_none_on_malformed(state_root: Path, payload: str):
    """Any shape mismatch → None. The host orchestrator owns this file;
    flight-assist is a non-owner reader and never raises on malformed
    snapshots — it just falls back to `home_address`."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / CURRENT_LOCATION_FILE).write_text(payload)
    assert read_current_location() is None


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
    state = _make_flight_state(flight_id=12345, code="XX123")
    write_flight_state(state)
    loaded = read_flight_state(12345)
    assert loaded is not None
    assert loaded["flight_id"] == 12345
    assert loaded["code"] == "XX123"
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_write_flight_state_requires_flight_id(state_root: Path):
    state = _make_flight_state()
    del state["flight_id"]
    with pytest.raises(ValueError, match="flight_id"):
        write_flight_state(state)


def test_write_flight_state_requires_integer_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id"):
        write_flight_state(_make_flight_state(flight_id="not-an-int"))


def test_write_flight_state_rejects_bool_for_int_field(state_root: Path):
    """`bool` is an int subclass; must be rejected for flight_id."""
    with pytest.raises(ValueError, match="bool"):
        write_flight_state(_make_flight_state(flight_id=True))


def test_write_flight_state_requires_code(state_root: Path):
    state = _make_flight_state()
    del state["code"]
    with pytest.raises(ValueError, match="'code'"):
        write_flight_state(state)


def test_write_flight_state_requires_phase_markers(state_root: Path):
    state = _make_flight_state()
    del state["phase_markers"]
    with pytest.raises(ValueError, match="phase_markers"):
        write_flight_state(state)


def test_write_flight_state_wrong_type_for_str_field(state_root: Path):
    """scheduled_dep_time must be str — passing an int raises ValueError."""
    with pytest.raises(ValueError, match="scheduled_dep_time"):
        write_flight_state(_make_flight_state(scheduled_dep_time=12345))


def test_write_flight_state_wrong_type_for_dict_field(state_root: Path):
    """phase_markers must be dict — passing a list raises ValueError."""
    with pytest.raises(ValueError, match="phase_markers"):
        write_flight_state(_make_flight_state(phase_markers=["a", "b"]))


def test_write_flight_state_optional_fields_may_be_omitted(state_root: Path):
    """last_snapshot / last_wake_at / last_wake_reason are optional."""
    state = _make_flight_state()
    del state["last_snapshot"]
    del state["last_wake_at"]
    del state["last_wake_reason"]
    write_flight_state(state)
    loaded = read_flight_state(state["flight_id"])
    assert loaded is not None
    assert "last_snapshot" not in loaded


def test_write_active_flights_rejects_non_list(state_root: Path):
    with pytest.raises(ValueError, match="must be a list"):
        write_active_flights("123")  # type: ignore[arg-type]


def test_write_active_flights_rejects_string_elements(state_root: Path):
    with pytest.raises(ValueError, match=r"flight_ids\[1\] is str"):
        write_active_flights([1, "2", 3])  # type: ignore[list-item]


def test_write_active_flights_rejects_bool_elements(state_root: Path):
    with pytest.raises(ValueError, match=r"flight_ids\[0\] is bool"):
        write_active_flights([True, 2])  # type: ignore[list-item]


def test_delete_flight_state_removes_existing_file(state_root: Path):
    write_flight_state(_make_flight_state(flight_id=999))
    assert read_flight_state(999) is not None
    assert delete_flight_state(999) is True
    assert read_flight_state(999) is None


def test_delete_flight_state_returns_false_when_missing(state_root: Path):
    assert delete_flight_state(404) is False


def test_read_flight_state_rejects_non_int_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id must be int"):
        read_flight_state("12345")  # type: ignore[arg-type]


def test_read_flight_state_rejects_bool_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id must be int"):
        read_flight_state(True)  # type: ignore[arg-type]


def test_delete_flight_state_rejects_non_int_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id must be int"):
        delete_flight_state("12345")  # type: ignore[arg-type]


def test_delete_flight_state_rejects_bool_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id must be int"):
        delete_flight_state(False)  # type: ignore[arg-type]


def test_write_config_rejects_non_string_home_address(state_root: Path):
    with pytest.raises(ValueError, match="home_address"):
        write_config({"home_address": 12345})  # type: ignore[dict-item]


def test_write_config_accepts_int_min_transfer_minutes(state_root: Path):
    write_config({"min_transfer_minutes": 60})
    loaded = read_config()
    assert loaded["min_transfer_minutes"] == 60


def test_write_config_rejects_string_min_transfer_minutes(state_root: Path):
    with pytest.raises(ValueError, match="min_transfer_minutes"):
        write_config({"min_transfer_minutes": "45"})  # type: ignore[dict-item]


def test_write_config_rejects_bool_min_transfer_minutes(state_root: Path):
    """bool is a subclass of int — must be explicitly rejected."""
    with pytest.raises(ValueError, match=r"min_transfer_minutes.*bool"):
        write_config({"min_transfer_minutes": True})  # type: ignore[dict-item]


def test_write_config_rejects_negative_min_transfer_minutes(state_root: Path):
    """Match the precheck and detect_connection_risks contracts."""
    with pytest.raises(ValueError, match="non-negative"):
        write_config({"min_transfer_minutes": -5})


def test_write_config_accepts_zero_min_transfer_minutes(state_root: Path):
    """Zero is the inclusive lower bound; non-negative means >= 0."""
    write_config({"min_transfer_minutes": 0})
    loaded = read_config()
    assert loaded["min_transfer_minutes"] == 0


def test_write_config_rejects_unknown_key(state_root: Path):
    with pytest.raises(ValueError, match="unknown field"):
        write_config({"home_address": "OK", "undocumented_key": "value"})


def test_write_config_drops_caller_schema_version_but_allows_it(state_root: Path):
    """Caller may supply schema_version; it's silently overridden, not rejected."""
    write_config({"home_address": "X", "schema_version": 999})
    loaded = read_config()
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_write_flight_state_rejects_phase_markers_missing_key(state_root: Path):
    state = _make_flight_state()
    del state["phase_markers"]["boarding_fired"]
    with pytest.raises(ValueError, match=r"boarding_fired"):
        write_flight_state(state)


def test_write_flight_state_rejects_phase_markers_unknown_key(state_root: Path):
    state = _make_flight_state()
    state["phase_markers"]["typo_fired"] = False
    with pytest.raises(ValueError, match="unknown keys"):
        write_flight_state(state)


def test_write_flight_state_rejects_non_bool_phase_marker(state_root: Path):
    state = _make_flight_state()
    state["phase_markers"]["boarding_fired"] = "yes"
    with pytest.raises(ValueError, match=r"phase_markers\['boarding_fired'\]"):
        write_flight_state(state)


def test_read_flight_state_rejects_record_with_missing_required_field(state_root: Path):
    """A hand-edited or corrupt file with schema_version: 1 but missing fields raises."""
    state_root.mkdir(parents=True)
    (state_root / "flight-12345.json").write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION, "flight_id": 12345})
    )
    with pytest.raises(StateError, match="missing required field"):
        read_flight_state(12345)


def test_read_flight_state_rejects_wrong_type_field(state_root: Path):
    """schema_version OK but a required field has the wrong type — StateError."""
    state_root.mkdir(parents=True)
    bad = _make_flight_state()
    bad["scheduled_dep_time"] = 12345  # str expected
    (state_root / "flight-12345.json").write_text(
        json.dumps({**bad, "schema_version": STATE_SCHEMA_VERSION})
    )
    with pytest.raises(StateError, match="scheduled_dep_time"):
        read_flight_state(12345)


def test_read_flight_state_rejects_empty_phase_markers(state_root: Path):
    """Read-side phase_markers structural check: empty dict raises."""
    state_root.mkdir(parents=True)
    bad = _make_flight_state()
    bad["phase_markers"] = {}
    (state_root / "flight-12345.json").write_text(
        json.dumps({**bad, "schema_version": STATE_SCHEMA_VERSION})
    )
    with pytest.raises(StateError, match="phase_markers missing keys"):
        read_flight_state(12345)


def test_read_flight_state_rejects_non_bool_phase_marker(state_root: Path):
    """Read-side phase_markers structural check: non-bool value raises."""
    state_root.mkdir(parents=True)
    bad = _make_flight_state()
    bad["phase_markers"]["boarding_fired"] = "yes"
    (state_root / "flight-12345.json").write_text(
        json.dumps({**bad, "schema_version": STATE_SCHEMA_VERSION})
    )
    with pytest.raises(StateError, match=r"phase_markers\['boarding_fired'\]"):
        read_flight_state(12345)


def test_read_flight_state_rejects_wrong_type_optional_field(state_root: Path):
    """Optional fields: when present, must match documented type."""
    state_root.mkdir(parents=True)
    bad = _make_flight_state()
    bad["last_wake_at"] = 12345  # str or None expected
    (state_root / "flight-12345.json").write_text(
        json.dumps({**bad, "schema_version": STATE_SCHEMA_VERSION})
    )
    with pytest.raises(StateError, match="last_wake_at"):
        read_flight_state(12345)


def test_write_flight_state_rejects_wrong_type_optional_field(state_root: Path):
    """write_flight_state validates optional-field types too."""
    state = _make_flight_state(last_wake_at=12345)  # str or None expected
    with pytest.raises(ValueError, match="last_wake_at"):
        write_flight_state(state)


def test_write_flight_state_allows_optional_field_as_none(state_root: Path):
    """None is acceptable for any optional field per the schema."""
    state = _make_flight_state(last_wake_at=None, last_wake_reason=None, last_snapshot=None)
    write_flight_state(state)
    loaded = read_flight_state(state["flight_id"])
    assert loaded["last_wake_at"] is None


def test_write_flight_state_rejects_unknown_top_level_field(state_root: Path):
    """The persisted JSON shape is bounded by state-schema.md — extras raise."""
    state = _make_flight_state(some_typo_field="value")
    with pytest.raises(ValueError, match="unknown fields"):
        write_flight_state(state)


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


def test_unknown_past_schema_version_raises_state_error(state_root: Path):
    """schema_version < current without a registered migration path raises."""
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(json.dumps({"schema_version": 0, "home_address": "X"}))
    with pytest.raises(StateError, match="no migration path"):
        read_config()


def test_v1_to_v2_migration_adds_connection_at_risk_marker(state_root: Path):
    """v1 per-flight state migrates: phase_markers gains connection_at_risk_fired."""
    state_root.mkdir(parents=True)
    v1_state = {
        "schema_version": 1,
        "flight_id": 12345,
        "code": "AA2414",
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
    (state_root / "flight-12345.json").write_text(json.dumps(v1_state))

    loaded = read_flight_state(12345)
    assert loaded is not None
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["phase_markers"]["connection_at_risk_fired"] is False
    # Migration rewrote the file: re-read from disk and verify persisted.
    raw = json.loads((state_root / "flight-12345.json").read_text())
    assert raw["schema_version"] == STATE_SCHEMA_VERSION
    assert raw["phase_markers"]["connection_at_risk_fired"] is False


def test_v1_to_v2_migration_idempotent_when_marker_present(state_root: Path):
    """A v1 record that already has the new marker key migrates without conflict."""
    state_root.mkdir(parents=True)
    v1_state = {
        "schema_version": 1,
        "flight_id": 12345,
        "code": "AA2414",
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
            "connection_at_risk_fired": True,  # caller pre-set
        },
        "last_wake_at": None,
        "last_wake_reason": None,
    }
    (state_root / "flight-12345.json").write_text(json.dumps(v1_state))
    loaded = read_flight_state(12345)
    assert loaded["phase_markers"]["connection_at_risk_fired"] is True


def test_v1_config_migration_bumps_version_without_shape_change(state_root: Path):
    """v1 config files have no shape change at v2 — just schema_version bump."""
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(
        json.dumps({"schema_version": 1, "home_address": "1 Old Loop"})
    )
    loaded = read_config()
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["home_address"] == "1 Old Loop"


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
    write_flight_state(_make_flight_state(flight_id=100, code="A"))
    write_flight_state(_make_flight_state(flight_id=200, code="B"))
    assert read_flight_state(100)["code"] == "A"
    assert read_flight_state(200)["code"] == "B"
    files = sorted(p.name for p in state_root.iterdir())
    assert files == ["flight-100.json", "flight-200.json"]
