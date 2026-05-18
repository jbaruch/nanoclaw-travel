"""Per-flight state file read/write for the flight-assist tile.

The precheck script reads + writes state across invocations to detect
deltas between byAir snapshots. State lives under
`/workspace/state/flight-assist/` in production; tests override the
directory via the `FLIGHT_ASSIST_STATE_DIR` environment variable.

Files written (all single-flat JSON, all carry `schema_version: 1`):

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

STATE_SCHEMA_VERSION = 1

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
    """Read a JSON file, verify schema_version. Return None if not present.

    Raises StateError on JSON corruption or on a schema_version newer
    than this module knows about (forward incompatibility).

    A schema_version OLDER than the current one falls through to the
    caller — only the owner skill migrates. Non-owner callers that get
    a mismatch must treat the record as missing.
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
    version = payload.get("schema_version")
    if version is None:
        raise StateError(
            f"state file {path} is missing schema_version — owner skill must rewrite or remove it"
        )
    if version > STATE_SCHEMA_VERSION:
        raise StateError(
            f"state file {path} has schema_version {version}, this module knows "
            f"only up to {STATE_SCHEMA_VERSION} — upgrade flight-assist before reading"
        )
    return payload


def read_config() -> dict | None:
    """Return the tile-wide config (home_address, etc.) or None if not set."""
    return _read_json_with_version(state_dir() / CONFIG_FILE)


def write_config(config: dict) -> None:
    """Persist the tile-wide config. `schema_version` is set automatically."""
    payload = {**config, "schema_version": STATE_SCHEMA_VERSION}
    _atomic_write_json(state_dir() / CONFIG_FILE, payload)


def read_active_flights() -> list[int]:
    """Return the list of currently-tracked flight_ids. Empty list if no index."""
    payload = _read_json_with_version(state_dir() / ACTIVE_FLIGHTS_FILE)
    if payload is None:
        return []
    flight_ids = payload.get("flight_ids", [])
    if not isinstance(flight_ids, list):
        raise StateError(
            f"active-flights.json has flight_ids of type {type(flight_ids).__name__}, expected list"
        )
    return [int(fid) for fid in flight_ids]


def write_active_flights(flight_ids: list[int]) -> None:
    """Persist the active-flights index."""
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "flight_ids": list(flight_ids),
    }
    _atomic_write_json(state_dir() / ACTIVE_FLIGHTS_FILE, payload)


def read_flight_state(flight_id: int) -> dict | None:
    """Return the per-flight state record or None on first run."""
    return _read_json_with_version(_flight_file(flight_id))


def write_flight_state(state: dict) -> None:
    """Persist a per-flight state record.

    Requires `flight_id` in the dict. `schema_version` is set
    automatically — overwrites any caller-supplied version.
    """
    flight_id = state.get("flight_id")
    if not isinstance(flight_id, int):
        raise ValueError(
            "write_flight_state: state dict must include integer 'flight_id' — "
            f"got {flight_id!r}"
        )
    payload = {**state, "schema_version": STATE_SCHEMA_VERSION}
    _atomic_write_json(_flight_file(flight_id), payload)


def delete_flight_state(flight_id: int) -> bool:
    """Remove a per-flight state file. Returns True if a file was deleted."""
    path = _flight_file(flight_id)
    if not path.exists():
        return False
    path.unlink()
    return True
