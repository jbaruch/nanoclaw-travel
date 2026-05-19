"""Per-flight state file read/write for the flight-assist tile.

The precheck script reads + writes state across invocations to detect
deltas between byAir snapshots. State lives under
`/workspace/state/flight-assist/` in production; tests override the
directory via the `FLIGHT_ASSIST_STATE_DIR` environment variable.

Files written (all JSON, all carry `schema_version: 2` at the top level):

    config.json                       — home_address, etc. (set via /setup)
    active-flights.json               — list of currently-tracked flight_ids
    flight-<flight_id>.json           — per-flight state record

Writes are atomic: write-to-tmp + os.replace, so a kill mid-write
doesn't leave a half-written file on disk.

Owner skill: `flight-assist` (this tile). Per `coding-policy:
stateful-artifacts`, only the owner skill migrates `schema_version`.
Reader skills (other tiles, future agent-side actions) treat any
mismatched `schema_version` as "no usable prior state".

See `state-schema.md` (sibling file) for the full per-record contract.

stdlib-only: `json` + `os` + `pathlib` per `coding-policy:
dependency-management` (Stdlib First).

Public API:
    # The skill bundle dir is added to sys.path at invocation time; this
    # module is imported by its bare name (matches nanoclaw-core's convention).
    from state import (
        STATE_SCHEMA_VERSION,
        read_config, write_config,
        read_active_flights, write_active_flights,
        read_flight_state, write_flight_state, delete_flight_state,
        state_dir,
    )

Reader skills (non-owner) that want to consult the latest snapshot
without migrating must check the `schema_version` against
`STATE_SCHEMA_VERSION` and treat a mismatch as "no usable prior
state" — never migrate from a reader path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_SCHEMA_VERSION = 2

_DEFAULT_STATE_DIR = "/workspace/state/flight-assist"
_STATE_DIR_ENV = "FLIGHT_ASSIST_STATE_DIR"

CONFIG_FILE = "config.json"
ACTIVE_FLIGHTS_FILE = "active-flights.json"
_FLIGHT_FILE_PREFIX = "flight-"
_FLIGHT_FILE_SUFFIX = ".json"


class StateError(Exception):
    """Raised on schema-mismatch or corrupt-state-file situations.

    Distinct from `FileNotFoundError` (which means "no prior state"):
    StateError means a file IS present but its contents are unusable.
    """


def state_dir() -> Path:
    """Return the active state directory, resolving via env var.

    Production: `/workspace/state/flight-assist/`. Tests override via
    `FLIGHT_ASSIST_STATE_DIR`. The directory is created on first write;
    callers should not pre-create it (the write_* helpers do it).
    """
    return Path(os.environ.get(_STATE_DIR_ENV, _DEFAULT_STATE_DIR))


def _validate_flight_id(flight_id: object, *, fn_name: str) -> None:
    """Reject anything that isn't a plain int (excluding bool).

    Used by every public function that takes a `flight_id` so the
    read / write / delete paths share the same contract per
    `coding-policy: stateful-artifacts`. Raising ValueError on a
    bad type is louder than silently forming `flight-True.json` or
    `flight-not-int.json` paths.
    """
    if not isinstance(flight_id, int) or isinstance(flight_id, bool):
        raise ValueError(
            f"{fn_name}: flight_id must be int, got " f"{type(flight_id).__name__} {flight_id!r}"
        )


def _flight_file(flight_id: int) -> Path:
    return state_dir() / f"{_FLIGHT_FILE_PREFIX}{flight_id}{_FLIGHT_FILE_SUFFIX}"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to a temp file in the same dir, then rename into place.

    Same-dir tmp ensures the rename is atomic on POSIX filesystems
    (cross-device renames are non-atomic). On Windows, os.replace is
    documented atomic for files on the same drive.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    os.replace(tmp, path)


def _read_json_with_version(path: Path) -> dict | None:
    """Read a JSON file, validate schema_version. Return None if missing.

    Raises StateError on any of: JSON corruption, non-object payload,
    missing schema_version, schema_version of a non-int type
    (including bool, since `bool` is a subclass of `int` in Python),
    or schema_version higher than the current module constant.

    schema_version equal to STATE_SCHEMA_VERSION returns the payload.
    schema_version LOWER than the current runs the owner-side migration
    in `_migrate` (which upgrade-and-rewrites the file before
    returning). Unknown lower versions raise StateError.
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as decode_err:
        raise StateError(
            f"state file {path} is not valid JSON — inspect and remove or restore: {decode_err}"
        ) from decode_err
    if not isinstance(payload, dict):
        raise StateError(f"state file {path} is not a JSON object — found {type(payload).__name__}")
    if "schema_version" not in payload:
        raise StateError(
            f"state file {path} is missing schema_version — owner skill must rewrite or remove it"
        )
    version = payload["schema_version"]
    # `bool` is a subclass of `int` in Python — exclude it so `True`/`False`
    # don't sneak through as schema_version values.
    if not isinstance(version, int) or isinstance(version, bool):
        raise StateError(
            f"state file {path} has schema_version of type {type(version).__name__} "
            f"({version!r}), expected int — remove the file and let the owner skill rewrite it"
        )
    if version > STATE_SCHEMA_VERSION:
        raise StateError(
            f"state file {path} has schema_version {version}, this module is at "
            f"{STATE_SCHEMA_VERSION} — upgrade flight-assist, or remove the file"
        )
    if version < STATE_SCHEMA_VERSION:
        payload = _migrate(payload, from_version=version, path=path)
    return payload


def _migrate(payload: dict, *, from_version: int, path: Path) -> dict:
    """Owner-side schema migration. Upgrades old payloads in place and rewrites.

    Migrations are additive only — non-owner readers see the latest
    schema after the owner skill (this module) reads any older file.
    Per `coding-policy: stateful-artifacts`, only the owner skill
    migrates; readers from other tiles get `StateError` on mismatch.

    Each branch handles one version transition. Re-running a migration
    on already-upgraded data is a no-op so the function is idempotent.
    """
    if from_version == 1:
        # v1 → v2: add `connection_at_risk_fired: False` to per-flight
        # phase_markers. Config and active-flights files have no shape
        # change at v2 — they only get a schema_version bump.
        phase_markers = payload.get("phase_markers")
        if isinstance(phase_markers, dict) and "connection_at_risk_fired" not in phase_markers:
            phase_markers["connection_at_risk_fired"] = False
        payload["schema_version"] = 2
        _atomic_write_json(path, payload)
        return payload
    # Unknown older version: refuse to silently pass through. The
    # migration table above is the authoritative list of known
    # upgrade paths; anything not listed is either corruption or a
    # downgrade gap and needs human intervention.
    raise StateError(
        f"state file {path} has schema_version {from_version}, no migration "
        f"path registered to reach {STATE_SCHEMA_VERSION} — remove the file or "
        f"restore a known version"
    )


def read_config() -> dict | None:
    """Return the tile-wide config (home_address, etc.) or None if not set."""
    return _read_json_with_version(state_dir() / CONFIG_FILE)


_CONFIG_OPTIONAL_FIELDS: dict[str, type] = {
    "home_address": str,
    "min_transfer_minutes": int,
}


def write_config(config: dict) -> None:
    """Persist the tile-wide config. `schema_version` is set automatically.

    Validates field types and rejects undocumented keys per the
    writer/reader contract in `state-schema.md`. The optional fields
    today are: `home_address` (str). Add new fields here when
    bumping the config schema; same place as the schema doc.

    Raises ValueError on:
    - Wrong type for a documented field
    - Any key not in `_CONFIG_OPTIONAL_FIELDS` (caller-supplied
      `schema_version` is allowed but is always overwritten by the
      canonical constant)
    """
    for key, value in config.items():
        if key == "schema_version":
            continue  # caller-supplied schema_version is dropped; canonical wins
        if key not in _CONFIG_OPTIONAL_FIELDS:
            raise ValueError(
                f"write_config: unknown field '{key}' — see state-schema.md "
                f"for the documented config shape, and bump the schema before "
                f"introducing new fields"
            )
        expected_type = _CONFIG_OPTIONAL_FIELDS[key]
        if expected_type is int and isinstance(value, bool):
            raise ValueError(f"write_config: field '{key}' is bool {value!r}, expected int")
        if not isinstance(value, expected_type):
            raise ValueError(
                f"write_config: field '{key}' is {type(value).__name__} "
                f"{value!r}, expected {expected_type.__name__}"
            )
    payload = {**config, "schema_version": STATE_SCHEMA_VERSION}
    _atomic_write_json(state_dir() / CONFIG_FILE, payload)


def read_active_flights() -> list[int]:
    """Return the list of currently-tracked flight_ids. Empty list if no index.

    Raises StateError if the field is missing, of the wrong shape, or
    contains any non-int element. No silent coercion: a stored
    `"123"` raises rather than parsing to 123, so the writer-side
    contract documented in `state-schema.md` is enforced strictly.
    """
    payload = _read_json_with_version(state_dir() / ACTIVE_FLIGHTS_FILE)
    if payload is None:
        return []
    if "flight_ids" not in payload:
        raise StateError(
            "active-flights.json is missing required field 'flight_ids' — "
            "remove the file and let the owner skill rewrite it"
        )
    flight_ids = payload["flight_ids"]
    if not isinstance(flight_ids, list):
        raise StateError(
            f"active-flights.json has flight_ids of type {type(flight_ids).__name__}, expected list"
        )
    for index, fid in enumerate(flight_ids):
        # `bool` is a subclass of `int`; exclude it so `True`/`False` don't
        # accidentally pass as flight IDs.
        if not isinstance(fid, int) or isinstance(fid, bool):
            raise StateError(
                f"active-flights.json flight_ids[{index}] is "
                f"{type(fid).__name__} {fid!r}, expected int — fix the file"
            )
    return list(flight_ids)


def write_active_flights(flight_ids: list[int]) -> None:
    """Persist the active-flights index.

    Validates the input matches the documented schema (list of plain int)
    before writing. `bool` is rejected even though it's an int subclass,
    so `[True]` doesn't sneak through. Strings, floats, and other
    iterables raise ValueError immediately rather than writing
    schema-invalid JSON that the reader will reject later.
    """
    if not isinstance(flight_ids, list):
        raise ValueError(
            "write_active_flights: flight_ids must be a list — " f"got {type(flight_ids).__name__}"
        )
    for index, fid in enumerate(flight_ids):
        if not isinstance(fid, int) or isinstance(fid, bool):
            raise ValueError(
                f"write_active_flights: flight_ids[{index}] is "
                f"{type(fid).__name__} {fid!r}, expected int"
            )
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "flight_ids": list(flight_ids),
    }
    _atomic_write_json(state_dir() / ACTIVE_FLIGHTS_FILE, payload)


def read_flight_state(flight_id: int) -> dict | None:
    """Return the per-flight state record or None on first run.

    Validates `flight_id` is a plain int (ValueError if not — same
    contract as write_flight_state and delete_flight_state) and the
    on-disk record contains every required field at the documented
    type (StateError if not). A corrupt or hand-edited file with
    `schema_version: 1` but missing fields is louder than a silent
    pass-through that would crash the caller deeper in the precheck
    pipeline.
    """
    _validate_flight_id(flight_id, fn_name="read_flight_state")
    payload = _read_json_with_version(_flight_file(flight_id))
    if payload is None:
        return None
    _validate_flight_state_payload(payload, source=_flight_file(flight_id))
    return payload


_OPTIONAL_FLIGHT_STATE_FIELDS: dict[str, tuple[type, ...]] = {
    # last_snapshot: object (validated only structurally — its sub-shape
    # comes from byair_client.get_flight() and may carry optional sub-fields)
    "last_snapshot": (dict, type(None)),
    "last_wake_at": (str, type(None)),
    "last_wake_reason": (str, type(None)),
}


def _validate_flight_state_payload(payload: dict, *, source: Path) -> None:
    """Verify a loaded flight-state record satisfies the documented contract.

    Same contract write_flight_state enforces on input, plus the optional
    fields validated for type-or-None. Raises StateError (not ValueError)
    because this is a read-side check on persisted data; the caller's
    recovery is "remove or restore the file", not "pass better arguments".
    """
    for field, expected_type in _REQUIRED_FLIGHT_STATE_FIELDS.items():
        if field not in payload:
            raise StateError(
                f"flight state file {source} is missing required field "
                f"'{field}' — remove the file and let the owner skill rewrite it"
            )
        value = payload[field]
        if expected_type is int and isinstance(value, bool):
            raise StateError(
                f"flight state file {source} field '{field}' is bool {value!r}, expected int"
            )
        if not isinstance(value, expected_type):
            raise StateError(
                f"flight state file {source} field '{field}' is "
                f"{type(value).__name__} {value!r}, expected {expected_type.__name__}"
            )

    # Phase markers structurally validated on read too, not just on write,
    # so a hand-edited file with `phase_markers: {}` raises rather than
    # silently passing through.
    try:
        _validate_phase_markers(payload["phase_markers"])
    except ValueError as marker_err:
        raise StateError(
            f"flight state file {source}: {marker_err} — " "remove or restore the file"
        ) from marker_err

    # Optional fields: when present, type-check against the documented shape.
    # Absent is fine; that's what "optional" means.
    for field, allowed_types in _OPTIONAL_FLIGHT_STATE_FIELDS.items():
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, allowed_types):
            type_names = "/".join(t.__name__ for t in allowed_types)
            raise StateError(
                f"flight state file {source} field '{field}' is "
                f"{type(value).__name__} {value!r}, expected {type_names}"
            )


_REQUIRED_FLIGHT_STATE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "flight_id": int,
    "code": str,
    "ownership": str,
    "trip_id": int,
    "scheduled_dep_time": str,
    "scheduled_arr_time": str,
    "dep_airport_id": int,
    "arr_airport_id": int,
    "last_polled_at": str,
    "phase_markers": dict,
}


def write_flight_state(state: dict) -> None:
    """Persist a per-flight state record.

    Validates the input matches the required-fields portion of the
    contract documented in `state-schema.md` before writing. Required
    keys (with types):

        flight_id (int), code (str), ownership (str), trip_id (int),
        scheduled_dep_time (str), scheduled_arr_time (str),
        dep_airport_id (int), arr_airport_id (int),
        last_polled_at (str), phase_markers (dict)

    Optional keys (no type check on this side; readers must tolerate
    missing or null): last_snapshot, last_wake_at, last_wake_reason.

    `schema_version` is overwritten by the canonical constant — any
    caller-supplied value is dropped.

    Raises ValueError on missing keys or wrong types so the writer
    never persists a record the reader would reject.
    """
    # Reject undocumented top-level keys so the persisted JSON shape is
    # bounded by state-schema.md. `schema_version` is allowed but
    # always overwritten below by the canonical constant.
    allowed_keys = (
        set(_REQUIRED_FLIGHT_STATE_FIELDS) | set(_OPTIONAL_FLIGHT_STATE_FIELDS) | {"schema_version"}
    )
    extra_keys = set(state.keys()) - allowed_keys
    if extra_keys:
        raise ValueError(
            f"write_flight_state: unknown fields {sorted(extra_keys)} — "
            f"see state-schema.md for the documented record shape, and bump "
            f"the schema before introducing new fields"
        )
    if "flight_id" not in state:
        raise ValueError(
            "write_flight_state: missing required field 'flight_id' — "
            "see state-schema.md for the full required set"
        )
    _validate_flight_id(state["flight_id"], fn_name="write_flight_state")
    for field, expected_type in _REQUIRED_FLIGHT_STATE_FIELDS.items():
        if field == "flight_id":
            continue  # handled above so the error names the function consistently
        if field not in state:
            raise ValueError(
                f"write_flight_state: missing required field '{field}' — "
                f"see state-schema.md for the full required set"
            )
        value = state[field]
        # Reject bool when expecting int (bool is an int subclass in Python).
        if expected_type is int and isinstance(value, bool):
            raise ValueError(f"write_flight_state: field '{field}' is bool {value!r}, expected int")
        if not isinstance(value, expected_type):
            raise ValueError(
                f"write_flight_state: field '{field}' is "
                f"{type(value).__name__} {value!r}, expected {expected_type.__name__}"
            )
    _validate_phase_markers(state["phase_markers"])

    # Optional fields: when present in the caller's dict, type-check them
    # against the documented shape so the writer never persists a record
    # the reader would reject.
    for field, allowed_types in _OPTIONAL_FLIGHT_STATE_FIELDS.items():
        if field not in state:
            continue
        value = state[field]
        if not isinstance(value, allowed_types):
            type_names = "/".join(t.__name__ for t in allowed_types)
            raise ValueError(
                f"write_flight_state: field '{field}' is "
                f"{type(value).__name__} {value!r}, expected {type_names}"
            )

    payload = {**state, "schema_version": STATE_SCHEMA_VERSION}
    _atomic_write_json(_flight_file(state["flight_id"]), payload)


_PHASE_MARKER_KEYS = frozenset(
    {
        "day_before_fired",
        "time_to_leave_fired",
        "boarding_fired",
        "arrival_logistics_fired",
        "landed_acknowledged",
        "connection_at_risk_fired",
    }
)


def _validate_phase_markers(phase_markers: dict) -> None:
    """Verify phase_markers has exactly the 6 documented keys, all bool.

    Per state-schema.md: phase_markers is `{day_before_fired,
    time_to_leave_fired, boarding_fired, arrival_logistics_fired,
    landed_acknowledged, connection_at_risk_fired}` — each a plain `bool`.
    No undocumented keys, no missing keys, no non-bool values.
    """
    actual_keys = set(phase_markers.keys())
    missing = _PHASE_MARKER_KEYS - actual_keys
    if missing:
        raise ValueError(
            f"write_flight_state: phase_markers missing keys "
            f"{sorted(missing)} — see state-schema.md"
        )
    extra = actual_keys - _PHASE_MARKER_KEYS
    if extra:
        raise ValueError(
            f"write_flight_state: phase_markers has unknown keys "
            f"{sorted(extra)} — see state-schema.md"
        )
    for key, value in phase_markers.items():
        if not isinstance(value, bool):
            raise ValueError(
                f"write_flight_state: phase_markers['{key}'] is "
                f"{type(value).__name__} {value!r}, expected bool"
            )


def delete_flight_state(flight_id: int) -> bool:
    """Remove a per-flight state file. Returns True if a file was deleted.

    Raises ValueError if `flight_id` isn't a plain int — same contract
    as read_flight_state and write_flight_state.
    """
    _validate_flight_id(flight_id, fn_name="delete_flight_state")
    path = _flight_file(flight_id)
    if not path.exists():
        return False
    path.unlink()
    return True
