"""Tests for skills/flight-assist/state.py.

Use the FLIGHT_ASSIST_STATE_DIR env var to redirect state to tmp_path,
keeping every test independent and self-cleaning per `coding-policy:
testing-standards` "Independence".
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, tzinfo
from pathlib import Path

import pytest

from helpers import must

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from state import (  # noqa: E402
    ACTIVE_FLIGHTS_FILE,
    CONFIG_FILE,
    CURRENT_LOCATION_FILE,
    CURRENT_LOCATION_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    StateError,
    delete_flight_state,
    list_flight_state_ids,
    read_active_flights,
    read_active_flights_snapshot,
    read_config,
    read_current_location,
    read_flight_state,
    read_flight_state_snapshot,
    resolve_live_origin,
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
            "gate_assignment_fired": False,
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
    loaded = must(read_config())
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_read_current_location_returns_none_when_missing(state_root: Path):
    """No `current-location.json` on disk → reader returns None
    (caller falls back to `home_address`)."""
    assert read_current_location() is None


def _valid_location_payload(**overrides) -> dict:
    """Build a minimally-valid `current-location.json` payload, with the
    canonical schema_version stamped automatically. Tests override
    individual fields to exercise the validator."""
    base = {
        "schema_version": CURRENT_LOCATION_SCHEMA_VERSION,
        "latitude": 59.6519,
        "longitude": 17.9186,
        "captured_at": "2026-05-20T11:42:11Z",
    }
    base.update(overrides)
    return base


def test_read_current_location_roundtrips_valid_payload(state_root: Path):
    """Well-formed snapshot is returned with the documented fields
    (schema_version is stripped from the returned dict — callers only
    need the geometry + timestamp)."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / CURRENT_LOCATION_FILE).write_text(json.dumps(_valid_location_payload()))
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
        json.dumps(_valid_location_payload(latitude="59.6519")),
        json.dumps(_valid_location_payload(latitude=True)),
        json.dumps({k: v for k, v in _valid_location_payload().items() if k != "captured_at"}),
        json.dumps(_valid_location_payload(latitude=999.0)),
        json.dumps(_valid_location_payload(longitude=999.0)),
        json.dumps({k: v for k, v in _valid_location_payload().items() if k != "schema_version"}),
        json.dumps(_valid_location_payload(schema_version=CURRENT_LOCATION_SCHEMA_VERSION + 1)),
        json.dumps(_valid_location_payload(schema_version=CURRENT_LOCATION_SCHEMA_VERSION - 1)),
        json.dumps(_valid_location_payload(schema_version=True)),
        json.dumps(_valid_location_payload(schema_version="1")),
        json.dumps(_valid_location_payload(captured_at="not-a-timestamp")),
        json.dumps(_valid_location_payload(captured_at="2026-05-20")),
        json.dumps(_valid_location_payload(captured_at="2026-05-20T11:42:11+02:00")),
        json.dumps(_valid_location_payload(captured_at="2026-05-20T11:42:11")),
    ],
    ids=[
        "malformed-json",
        "list-payload",
        "lat-as-string",
        "lat-as-bool",
        "missing-captured_at",
        "lat-out-of-range",
        "lng-out-of-range",
        "missing-schema_version",
        "schema_version-too-new",
        "schema_version-too-old",
        "schema_version-as-bool",
        "schema_version-as-string",
        "captured_at-unparseable",
        "captured_at-date-only",
        "captured_at-non-utc-offset",
        "captured_at-naive-no-tz",
    ],
)
def test_read_current_location_returns_none_on_malformed(state_root: Path, payload: str):
    """Any shape mismatch → None. The host orchestrator owns this file;
    flight-assist is a non-owner reader and never raises on malformed
    snapshots, missing or mismatched `schema_version`, or non-UTF-8
    bytes — it just falls back to `home_address`."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / CURRENT_LOCATION_FILE).write_text(payload)
    assert read_current_location() is None


def test_read_current_location_returns_none_on_non_utf8_bytes(state_root: Path):
    """A truncated UTF-8 sequence (or any non-UTF-8 byte stream) →
    `read_text(encoding='utf-8')` raises `UnicodeDecodeError`; the
    reader catches it and returns None instead of propagating, so a
    partial host-side write never crashes the precheck."""
    state_root.mkdir(parents=True, exist_ok=True)
    # The first byte of a 4-byte UTF-8 codepoint (`\xf0`) with nothing
    # behind it — a real truncation shape we'd see if the host got
    # SIGKILLed mid-write before atomic-rename.
    (state_root / CURRENT_LOCATION_FILE).write_bytes(b"\xf0")
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
        # deliberate wrong type: rejection is the behavior under test
        write_flight_state(_make_flight_state(flight_id="not-an-int"))  # type: ignore[arg-type]


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
        # deliberate wrong type: rejection is the behavior under test
        write_active_flights("123")  # type: ignore[arg-type]


def test_write_active_flights_rejects_string_elements(state_root: Path):
    with pytest.raises(ValueError, match=r"flight_ids\[1\] is str"):
        # deliberate wrong type: rejection is the behavior under test
        write_active_flights([1, "2", 3])  # type: ignore[list-item]


def test_write_active_flights_rejects_bool_elements(state_root: Path):
    with pytest.raises(ValueError, match=r"flight_ids\[0\] is bool"):
        # deliberate wrong type: rejection is the behavior under test
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
        # deliberate wrong type: rejection is the behavior under test
        read_flight_state("12345")  # type: ignore[arg-type]


def test_read_flight_state_rejects_bool_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id must be int"):
        # deliberate wrong type: rejection is the behavior under test
        read_flight_state(True)  # type: ignore[arg-type]


def test_delete_flight_state_rejects_non_int_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id must be int"):
        # deliberate wrong type: rejection is the behavior under test
        delete_flight_state("12345")  # type: ignore[arg-type]


def test_delete_flight_state_rejects_bool_flight_id(state_root: Path):
    with pytest.raises(ValueError, match="flight_id must be int"):
        # deliberate wrong type: rejection is the behavior under test
        delete_flight_state(False)  # type: ignore[arg-type]


def test_write_config_rejects_non_string_home_address(state_root: Path):
    with pytest.raises(ValueError, match="home_address"):
        # deliberate wrong type: rejection is the behavior under test
        write_config({"home_address": 12345})  # type: ignore[dict-item]


def test_write_config_accepts_int_min_transfer_minutes(state_root: Path):
    write_config({"min_transfer_minutes": 60})
    loaded = must(read_config())
    assert loaded["min_transfer_minutes"] == 60


def test_write_config_rejects_string_min_transfer_minutes(state_root: Path):
    with pytest.raises(ValueError, match="min_transfer_minutes"):
        # deliberate wrong type: rejection is the behavior under test
        write_config({"min_transfer_minutes": "45"})  # type: ignore[dict-item]


def test_write_config_rejects_bool_min_transfer_minutes(state_root: Path):
    """bool is a subclass of int — must be explicitly rejected."""
    with pytest.raises(ValueError, match=r"min_transfer_minutes.*bool"):
        # deliberate wrong type: rejection is the behavior under test
        write_config({"min_transfer_minutes": True})  # type: ignore[dict-item]


def test_write_config_rejects_negative_min_transfer_minutes(state_root: Path):
    """Match the precheck and detect_connection_risks contracts."""
    with pytest.raises(ValueError, match="non-negative"):
        write_config({"min_transfer_minutes": -5})


def test_write_config_accepts_zero_min_transfer_minutes(state_root: Path):
    """Zero is the inclusive lower bound; non-negative means >= 0."""
    write_config({"min_transfer_minutes": 0})
    loaded = must(read_config())
    assert loaded["min_transfer_minutes"] == 0


def test_write_config_rejects_unknown_key(state_root: Path):
    with pytest.raises(ValueError, match="unknown field"):
        write_config({"home_address": "OK", "undocumented_key": "value"})


def test_write_config_drops_caller_schema_version_but_allows_it(state_root: Path):
    """Caller may supply schema_version; it's silently overridden, not rejected."""
    write_config({"home_address": "X", "schema_version": 999})
    loaded = must(read_config())
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
    loaded = must(read_flight_state(state["flight_id"]))
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

    loaded = must(read_flight_state(12345))
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
    loaded = must(read_flight_state(12345))
    assert loaded["phase_markers"]["connection_at_risk_fired"] is True


def test_v1_config_migration_bumps_version_without_shape_change(state_root: Path):
    """v1 config files have no shape change at v2 — just schema_version bump."""
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(
        json.dumps({"schema_version": 1, "home_address": "1 Old Loop"})
    )
    loaded = must(read_config())
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["home_address"] == "1 Old Loop"


def _v2_flight_state(**overrides) -> dict:
    """Build a raw on-disk v2 per-flight record (pre-calendar_events).

    Mirrors the v2 shape: phase_markers carries connection_at_risk_fired
    (added at v2) but there is no calendar_events map (added at v3).
    """
    state = {
        "schema_version": 2,
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
            "connection_at_risk_fired": False,
        },
        "last_wake_at": None,
        "last_wake_reason": None,
    }
    state.update(overrides)
    return state


def test_v2_to_v3_migration_adds_calendar_events(state_root: Path):
    """v2 per-flight state migrates: gains an empty calendar_events map."""
    state_root.mkdir(parents=True)
    (state_root / "flight-12345.json").write_text(json.dumps(_v2_flight_state()))

    loaded = must(read_flight_state(12345))
    assert loaded is not None
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["calendar_events"] == {}
    # Migration rewrote the file at the current version.
    raw = json.loads((state_root / "flight-12345.json").read_text())
    assert raw["schema_version"] == STATE_SCHEMA_VERSION
    assert raw["calendar_events"] == {}


def test_v2_to_v3_migration_idempotent_when_calendar_events_present(state_root: Path):
    """A v2 record already carrying calendar_events keeps its entries on migration."""
    state_root.mkdir(parents=True)
    existing = {
        "boarding": {
            "event_id": "abc123",
            "calendar_id": "primary",
            "managed": "created",
            "synced_signature": "2026-05-17T12:24:00-07:00/2026-05-17T13:00:00-07:00",
        }
    }
    (state_root / "flight-12345.json").write_text(
        json.dumps(_v2_flight_state(calendar_events=existing))
    )
    loaded = must(read_flight_state(12345))
    assert loaded["calendar_events"] == existing


def test_v2_config_migration_bumps_version_without_shape_change(state_root: Path):
    """v2 config files have no shape change at v3 — bump only, no calendar_events."""
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(
        json.dumps({"schema_version": 2, "home_address": "1 Old Loop", "min_transfer_minutes": 60})
    )
    loaded = must(read_config())
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["home_address"] == "1 Old Loop"
    assert "calendar_events" not in loaded


def test_v3_to_v4_config_migration_bumps_version_without_shape_change(state_root: Path):
    """v3 config files have no shape change at v4 — schema_version bump only.

    The v4 calendar-reconcile fields (byair_calendar_name / byair_calendar_id)
    are optional and absent-tolerant, so an old config without them migrates
    cleanly and gains no keys.
    """
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(
        json.dumps({"schema_version": 3, "home_address": "1 Old Loop", "min_transfer_minutes": 60})
    )
    loaded = must(read_config())
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["home_address"] == "1 Old Loop"
    assert loaded["min_transfer_minutes"] == 60
    assert "byair_calendar_name" not in loaded
    assert "byair_calendar_id" not in loaded


def test_config_round_trips_byair_calendar_fields(state_root: Path):
    """The v4 calendar-reconcile config fields write and read back unchanged."""
    state_root.mkdir(parents=True)
    write_config(
        {
            "home_address": "1 Infinite Loop, Cupertino, CA",
            "byair_calendar_name": "Flighty Flights",
            "byair_calendar_id": "c_abc123@group.calendar.google.com",
        }
    )
    loaded = must(read_config())
    assert loaded["byair_calendar_name"] == "Flighty Flights"
    assert loaded["byair_calendar_id"] == "c_abc123@group.calendar.google.com"
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_write_config_rejects_non_string_byair_calendar_id(state_root: Path):
    """byair_calendar_id is a string field — a non-string is rejected at the writer."""
    state_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="byair_calendar_id"):
        write_config({"byair_calendar_id": 12345})


def test_v4_to_v5_config_migration_bumps_version_without_shape_change(state_root: Path):
    """v4 config files have no shape change at v5 — schema_version bump only.

    The v5 airport-clearance fields are optional and absent-tolerant, so an old
    config without them migrates cleanly and gains no keys.
    """
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(
        json.dumps({"schema_version": 4, "home_address": "1 Old Loop", "min_transfer_minutes": 60})
    )
    loaded = must(read_config())
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["home_address"] == "1 Old Loop"
    assert loaded["min_transfer_minutes"] == 60
    assert "airport_clearance_domestic_minutes" not in loaded


def _v5_flight_state(**overrides) -> dict:
    """Build a raw on-disk v5 per-flight record (pre-gate_assignment_fired)."""
    base = {
        "schema_version": 5,
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
        "calendar_events": {},
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


def test_v5_to_v6_migration_adds_gate_assignment_marker(state_root: Path):
    """v5 per-flight state migrates: phase_markers gains gate_assignment_fired."""
    state_root.mkdir(parents=True)
    (state_root / "flight-12345.json").write_text(json.dumps(_v5_flight_state()))

    loaded = must(read_flight_state(12345))
    assert loaded is not None
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["phase_markers"]["gate_assignment_fired"] is False
    # Migration rewrote the file: re-read from disk and verify persisted.
    raw = json.loads((state_root / "flight-12345.json").read_text())
    assert raw["schema_version"] == STATE_SCHEMA_VERSION
    assert raw["phase_markers"]["gate_assignment_fired"] is False


def test_v5_to_v6_migration_idempotent_when_marker_present(state_root: Path):
    """A v5 record that already carries the marker migrates without clobbering it."""
    state_root.mkdir(parents=True)
    record = _v5_flight_state()
    record["phase_markers"]["gate_assignment_fired"] = True  # caller pre-set
    (state_root / "flight-12345.json").write_text(json.dumps(record))

    loaded = must(read_flight_state(12345))
    assert loaded["phase_markers"]["gate_assignment_fired"] is True


def test_v5_to_v6_config_migration_bumps_version_without_shape_change(state_root: Path):
    """v5 config files have no shape change at v6 — schema_version bump only."""
    state_root.mkdir(parents=True)
    (state_root / CONFIG_FILE).write_text(
        json.dumps({"schema_version": 5, "home_address": "1 Old Loop", "min_transfer_minutes": 60})
    )
    loaded = must(read_config())
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["home_address"] == "1 Old Loop"
    assert "gate_assignment_fired" not in loaded


def test_v5_to_v6_migration_scopes_phase_markers_by_filename(state_root: Path):
    """A non-flight file carrying a stray phase_markers key is NOT given
    gate_assignment_fired. The v5→v6 step scopes by filename (flight-<id>.json),
    matching v2→v3, so a config/active-flights file keeps its shape."""
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(
        json.dumps({"schema_version": 5, "flight_ids": [12345], "phase_markers": {"x": True}})
    )
    read_active_flights()  # owner-path read migrates and rewrites at v6
    raw = json.loads((state_root / ACTIVE_FLIGHTS_FILE).read_text())
    assert raw["schema_version"] == STATE_SCHEMA_VERSION
    assert "gate_assignment_fired" not in raw["phase_markers"]


def test_config_round_trips_airport_clearance_fields(state_root: Path):
    """The v5 airport-clearance config fields write and read back unchanged."""
    state_root.mkdir(parents=True)
    write_config(
        {
            "airport_clearance_domestic_minutes": 60,
            "airport_clearance_international_minutes": 120,
            "airport_post_arrival_domestic_minutes": 20,
            "airport_post_arrival_intl_us_minutes": 40,
            "airport_post_arrival_intl_abroad_minutes": 60,
        }
    )
    loaded = must(read_config())
    assert loaded["airport_clearance_domestic_minutes"] == 60
    assert loaded["airport_clearance_international_minutes"] == 120
    assert loaded["airport_post_arrival_domestic_minutes"] == 20
    assert loaded["airport_post_arrival_intl_us_minutes"] == 40
    assert loaded["airport_post_arrival_intl_abroad_minutes"] == 60
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION


def test_write_config_accepts_zero_airport_clearance(state_root: Path):
    state_root.mkdir(parents=True)
    write_config({"airport_clearance_domestic_minutes": 0})
    assert must(read_config())["airport_clearance_domestic_minutes"] == 0


def test_write_config_rejects_negative_airport_clearance(state_root: Path):
    state_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="airport_clearance_domestic_minutes"):
        write_config({"airport_clearance_domestic_minutes": -5})


def test_write_config_rejects_bool_airport_post_arrival(state_root: Path):
    state_root.mkdir(parents=True)
    with pytest.raises(ValueError, match=r"airport_post_arrival_intl_us_minutes.*bool"):
        write_config({"airport_post_arrival_intl_us_minutes": True})


def test_v2_to_v3_migration_scopes_calendar_events_by_filename(state_root: Path):
    """A non-flight file carrying a stray flight_id key is NOT given calendar_events.

    The v2→v3 step scopes by filename (flight-<id>.json), not by the
    presence of a flight_id key, so a config/active-flights file (or any
    future record) that happens to carry flight_id keeps its shape.
    """
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(
        json.dumps({"schema_version": 2, "flight_ids": [12345], "flight_id": 999})
    )
    read_active_flights()  # owner-path read migrates and rewrites at v3
    raw = json.loads((state_root / ACTIVE_FLIGHTS_FILE).read_text())
    assert raw["schema_version"] == STATE_SCHEMA_VERSION
    assert "calendar_events" not in raw


def test_v1_to_v3_chained_migration_adds_both_keys(state_root: Path):
    """A v1 per-flight record steps v1→v2→v3 in one read: both new keys appear."""
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

    loaded = must(read_flight_state(12345))
    assert loaded["schema_version"] == STATE_SCHEMA_VERSION
    assert loaded["phase_markers"]["connection_at_risk_fired"] is False
    assert loaded["calendar_events"] == {}


def test_write_then_read_flight_state_with_calendar_events_roundtrips(state_root: Path):
    """calendar_events present on write survives the read round-trip unchanged."""
    events = {
        "boarding": {
            "event_id": "abc123",
            "calendar_id": "primary",
            "managed": "created",
            "synced_signature": "2026-05-17T12:24:00-07:00/2026-05-17T13:00:00-07:00",
        },
        "flight": {
            "event_id": "ghi789",
            "calendar_id": "c_byair@group.calendar.google.com",
            "managed": "adopted",
            "synced_signature": "2026-05-17T13:00:00-07:00/2026-05-17T15:02:00-07:00",
        },
    }
    write_flight_state(_make_flight_state(calendar_events=events))
    loaded = must(read_flight_state(12345))
    assert loaded["calendar_events"] == events


def test_write_flight_state_rejects_non_dict_calendar_events(state_root: Path):
    """calendar_events is validated structurally (object) on write."""
    with pytest.raises(ValueError, match="calendar_events"):
        write_flight_state(_make_flight_state(calendar_events=["not", "a", "dict"]))


def test_read_flight_state_rejects_non_dict_calendar_events(state_root: Path):
    """Persisted record at current schema with non-object calendar_events raises StateError."""
    state_root.mkdir(parents=True)
    bad = _make_flight_state(calendar_events="oops")
    (state_root / "flight-12345.json").write_text(
        json.dumps({**bad, "schema_version": STATE_SCHEMA_VERSION})
    )
    with pytest.raises(StateError, match="calendar_events"):
        read_flight_state(12345)


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
    assert must(read_flight_state(100))["code"] == "A"
    assert must(read_flight_state(200))["code"] == "B"
    files = sorted(p.name for p in state_root.iterdir())
    assert files == ["flight-100.json", "flight-200.json"]


# --------------------------------------------------------------------
# Non-owner reader API — read_active_flights_snapshot,
# read_flight_state_snapshot.  These functions exist so non-owner skills
# (sync-tripit, future cross-plugin readers) can consult the latest
# snapshot without triggering owner-side schema migrations. Contract
# per `coding-policy: stateful-artifacts`: schema_version mismatch
# returns "no usable prior state" instead of migrating; the next
# owner-skill invocation performs the upgrade.
# --------------------------------------------------------------------


def test_read_active_flights_snapshot_returns_empty_when_missing(state_root: Path):
    """Missing file → []. Matches the owner-side function's no-state branch."""
    assert read_active_flights_snapshot() == []


def test_read_active_flights_snapshot_returns_current_payload(state_root: Path):
    """When schema_version matches, snapshot reader returns the payload."""
    write_active_flights([111, 222])
    assert read_active_flights_snapshot() == [111, 222]


def test_read_active_flights_snapshot_skips_old_schema_without_migrating(state_root: Path):
    """Non-owner reader contract: an older schema_version returns [] (no
    usable prior state) and MUST NOT rewrite the file."""
    state_root.mkdir(parents=True)
    legacy_payload = {"schema_version": STATE_SCHEMA_VERSION - 1, "flight_ids": [999]}
    path = state_root / ACTIVE_FLIGHTS_FILE
    path.write_text(json.dumps(legacy_payload))
    before_bytes = path.read_bytes()

    assert read_active_flights_snapshot() == []
    # File on disk is unchanged — no migration was performed.
    assert path.read_bytes() == before_bytes


def test_read_active_flights_snapshot_raises_state_error_on_corruption(state_root: Path):
    """Integrity failures (corrupt JSON, future schema_version) still
    raise — the snapshot reader only short-circuits old-schema cases."""
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text("{not valid json")
    with pytest.raises(StateError):
        read_active_flights_snapshot()


def test_read_active_flights_snapshot_raises_on_future_schema_version(state_root: Path):
    state_root.mkdir(parents=True)
    (state_root / ACTIVE_FLIGHTS_FILE).write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION + 1, "flight_ids": []})
    )
    with pytest.raises(StateError):
        read_active_flights_snapshot()


def test_read_flight_state_snapshot_returns_none_when_missing(state_root: Path):
    """Missing per-flight file → None."""
    assert read_flight_state_snapshot(12345) is None


def test_read_flight_state_snapshot_returns_current_payload(state_root: Path):
    """When schema_version matches, snapshot reader returns the payload."""
    write_flight_state(_make_flight_state(flight_id=12345, code="AA2414"))
    loaded = read_flight_state_snapshot(12345)
    assert loaded is not None
    assert loaded["code"] == "AA2414"


def test_read_flight_state_snapshot_skips_old_schema_without_migrating(state_root: Path):
    """Non-owner reader contract for per-flight state: older
    schema_version returns None without rewriting the file."""
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
    path = state_root / "flight-12345.json"
    path.write_text(json.dumps(v1_state))
    before_bytes = path.read_bytes()

    assert read_flight_state_snapshot(12345) is None
    # File on disk unchanged — no v1→v2 migration performed by the reader.
    assert path.read_bytes() == before_bytes
    # Sanity: the owner-side reader would have migrated and rewritten.
    assert must(read_flight_state(12345))["phase_markers"]["connection_at_risk_fired"] is False
    assert path.read_bytes() != before_bytes


def test_read_flight_state_snapshot_rejects_non_int_flight_id(state_root: Path):
    """Same flight_id validation as the owner-side reader."""
    with pytest.raises(ValueError):
        # deliberate wrong type: rejection is the behavior under test
        read_flight_state_snapshot("12345")  # type: ignore[arg-type]


def test_list_flight_state_ids_returns_empty_when_dir_missing(state_root: Path):
    """No state directory yet (first run) → empty list, never an error."""
    assert list_flight_state_ids() == []


def test_list_flight_state_ids_enumerates_sorted(state_root: Path):
    """Every flight-<id>.json on disk is enumerated, regardless of the
    active-flights index, sorted ascending — the tombstone sweep needs to
    see switched-away flights the index no longer lists."""
    write_flight_state(_make_flight_state(flight_id=12))
    write_flight_state(_make_flight_state(flight_id=3))
    write_flight_state(_make_flight_state(flight_id=200))
    assert list_flight_state_ids() == [3, 12, 200]


def test_list_flight_state_ids_skips_non_flight_files(state_root: Path):
    """config.json / active-flights.json / current-location.json and any
    flight-*.json whose middle segment is not an integer are skipped."""
    write_flight_state(_make_flight_state(flight_id=7))
    write_config({"home_address": "1 Infinite Loop"})
    write_active_flights([7])
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / CURRENT_LOCATION_FILE).write_text("{}")
    (state_root / "flight-notanint.json").write_text("{}")
    (state_root / "flight-.json").write_text("{}")
    assert list_flight_state_ids() == [7]


# --- resolve_live_origin (the shared origin ladder) ----------------------------

_NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


def _write_location(state_root: Path, captured_at: str) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    payload = _valid_location_payload(captured_at=captured_at)
    (state_root / CURRENT_LOCATION_FILE).write_text(json.dumps(payload))


def test_resolve_live_origin_uses_fresh_location(state_root: Path):
    # 15 min old (within the 30-min window) → the live lat,lng wins.
    _write_location(state_root, "2026-05-20T11:45:00Z")
    assert resolve_live_origin("1 Infinite Loop", now=_NOW) == "59.6519,17.9186"


def test_resolve_live_origin_falls_back_to_home_when_stale(state_root: Path):
    # 60 min old → past the window → the static home address.
    _write_location(state_root, "2026-05-20T11:00:00Z")
    assert resolve_live_origin("1 Infinite Loop", now=_NOW) == "1 Infinite Loop"


def test_resolve_live_origin_falls_back_when_location_is_in_the_future(state_root: Path):
    # Future-dated snapshot (negative age) is not "fresh" → home address.
    _write_location(state_root, "2026-05-20T12:30:00Z")
    assert resolve_live_origin("1 Infinite Loop", now=_NOW) == "1 Infinite Loop"


def test_resolve_live_origin_returns_home_when_no_location(state_root: Path):
    assert resolve_live_origin("1 Infinite Loop", now=_NOW) == "1 Infinite Loop"


def test_resolve_live_origin_returns_none_when_no_location_and_no_home(state_root: Path):
    assert resolve_live_origin(None, now=_NOW) is None


def test_resolve_live_origin_rejects_naive_now(state_root: Path):
    # A naive `now` is a caller bug — fail fast with a clear message rather than
    # TypeError mid-subtraction against the aware captured_at.
    naive = datetime(2026, 5, 20, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_live_origin("1 Infinite Loop", now=naive)


class _OffsetNaiveTZ(tzinfo):
    """A tzinfo whose utcoffset() is None — tzinfo is set but the instant is
    still offset-naive, which would TypeError on subtraction."""

    def utcoffset(self, dt):
        return None

    def tzname(self, dt):
        return None

    def dst(self, dt):
        return None


def test_resolve_live_origin_rejects_offset_naive_now(state_root: Path):
    # tzinfo is non-None but carries no offset → still rejected with the clear
    # ValueError, not a TypeError mid-subtraction.
    offset_naive = datetime(2026, 5, 20, 12, 0, 0, tzinfo=_OffsetNaiveTZ())
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_live_origin("1 Infinite Loop", now=offset_naive)
