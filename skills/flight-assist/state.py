"""Per-flight state file read/write for the flight-assist tile.

The precheck script reads + writes state across invocations to detect
deltas between byAir snapshots. State lives under
`/workspace/state/flight-assist/` in production; tests override the
directory via the `FLIGHT_ASSIST_STATE_DIR` environment variable.

Files written (all JSON, all carry `schema_version: 3` at the top level):

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
        # Non-owner reader entry points (snapshot semantics — never
        # invoke _migrate, so calling tiles do not rewrite owner state):
        read_active_flights_snapshot,
        read_flight_state_snapshot,
        state_dir,
    )

Non-owner reader contract: any tile that reads (but does not own) this
state — sync-tripit, future agent-side composition, other tiles — MUST
use the `*_snapshot` entry points. They treat a schema_version BELOW
the current `STATE_SCHEMA_VERSION` as "no usable prior state" (return
None / []) without rewriting the file, satisfying `coding-policy:
stateful-artifacts`'s single-owner migration rule. A schema_version
ABOVE the current still raises `StateError` (forward incompatibility,
not an old-state case) — operators need to upgrade the consumer tile,
not be told there's nothing on disk.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_SCHEMA_VERSION = 3

_DEFAULT_STATE_DIR = "/workspace/state/flight-assist"
_STATE_DIR_ENV = "FLIGHT_ASSIST_STATE_DIR"

CONFIG_FILE = "config.json"
ACTIVE_FLIGHTS_FILE = "active-flights.json"
CURRENT_LOCATION_FILE = "current-location.json"
CURRENT_LOCATION_SCHEMA_VERSION = 1
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


def _read_json_with_version(path: Path, *, migrate: bool = True) -> dict | None:
    """Read a JSON file, validate schema_version. Return None if missing.

    Raises StateError on any of: JSON corruption, non-object payload,
    missing schema_version, schema_version of a non-int type
    (including bool, since `bool` is a subclass of `int` in Python),
    or schema_version higher than the current module constant.

    schema_version equal to STATE_SCHEMA_VERSION returns the payload.
    schema_version HIGHER than current raises StateError regardless of
    the `migrate` kwarg (forward incompatibility is never an old-state
    case). schema_version LOWER than the current is handled per the
    `migrate` kwarg:
    - `migrate=True` (owner path): runs `_migrate`, which upgrade-and-
      rewrites the file before returning. Unknown lower versions raise
      StateError.
    - `migrate=False` (non-owner reader path): returns None per
      `coding-policy: stateful-artifacts` — non-owner readers must NOT
      migrate. Treat as "no usable prior state" and let the next
      owner-skill invocation perform the upgrade.
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
        if not migrate:
            return None
        payload = _migrate(payload, from_version=version, path=path)
    return payload


def _migrate(payload: dict, *, from_version: int, path: Path) -> dict:
    """Owner-side schema migration. Upgrades old payloads in place and rewrites.

    Migrations are additive only — non-owner readers see the latest
    schema after the owner skill (this module) reads any older file.
    Per `coding-policy: stateful-artifacts`, only the owner skill
    migrates; readers from other tiles get `StateError` on mismatch.

    Each branch handles one version transition and they chain: a v1
    record steps 1→2→3 in a single call. Re-running a migration on
    already-upgraded data is a no-op (each branch guards on the key it
    adds) so the function is idempotent.
    """
    version = from_version
    if version == 1:
        # v1 → v2: add `connection_at_risk_fired: False` to per-flight
        # phase_markers. Config and active-flights files have no shape
        # change at v2 — they only get a schema_version bump.
        phase_markers = payload.get("phase_markers")
        if isinstance(phase_markers, dict) and "connection_at_risk_fired" not in phase_markers:
            phase_markers["connection_at_risk_fired"] = False
        version = 2
    if version == 2:
        # v2 → v3: per-flight records gain an empty `calendar_events`
        # map (flight-assist-owned/adopted Google Calendar event IDs —
        # see state-schema.md). Keyed off the per-flight `flight_id`
        # field; config and active-flights files have no shape change
        # at v3 and only get a schema_version bump.
        if "flight_id" in payload and "calendar_events" not in payload:
            payload["calendar_events"] = {}
        version = 3
    if version != STATE_SCHEMA_VERSION:
        # Unknown older version: refuse to silently pass through. The
        # branches above are the authoritative list of known upgrade
        # paths; anything not reaching the current version is either
        # corruption or a downgrade gap and needs human intervention.
        raise StateError(
            f"state file {path} has schema_version {from_version}, no migration "
            f"path registered to reach {STATE_SCHEMA_VERSION} — remove the file or "
            f"restore a known version"
        )
    payload["schema_version"] = STATE_SCHEMA_VERSION
    _atomic_write_json(path, payload)
    return payload


def read_config() -> dict | None:
    """Return the tile-wide config (home_address, etc.) or None if not set."""
    return _read_json_with_version(state_dir() / CONFIG_FILE)


def read_current_location() -> dict | None:
    """Return the latest user-location snapshot, or None when unavailable.

    Path: `state_dir()/current-location.json`. Owner is the host
    orchestrator (which writes this file as the user's location updates
    via Telegram live-location or message metadata); flight-assist is a
    non-owner reader per `coding-policy: stateful-artifacts`. The
    helper validates the documented shape and returns None on any
    mismatch (missing file, malformed JSON, non-UTF-8 bytes, missing
    required field, wrong type, `schema_version` not equal to
    `CURRENT_LOCATION_SCHEMA_VERSION`) instead of raising — origin
    resolution falls back to `home_address` when this returns None.

    Required fields:

        schema_version  (int, must equal CURRENT_LOCATION_SCHEMA_VERSION)
        latitude        (float, in [-90, 90])
        longitude       (float, in [-180, 180])
        captured_at     (ISO-8601 UTC string)

    Per the non-owner reader contract in `coding-policy:
    stateful-artifacts`: a `schema_version` mismatch returns None
    rather than migrating. The orchestrator is the sole writer.

    Freshness (age relative to `now`) is the caller's responsibility —
    this returns whatever is on disk, parsed and shape-validated only.
    """
    path = state_dir() / CURRENT_LOCATION_FILE
    if not path.exists():
        return None
    # `read_text` raises `UnicodeDecodeError` on non-UTF-8 bytes — the
    # host-owned file could be cut mid-write or land non-UTF-8 from a
    # future host shape, both of which should resolve to "no usable
    # snapshot" rather than propagating into the precheck's outer
    # try/except.
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("schema_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != CURRENT_LOCATION_SCHEMA_VERSION
    ):
        return None
    lat = payload.get("latitude")
    lng = payload.get("longitude")
    captured = payload.get("captured_at")
    # `bool` is an `int` subclass in Python — exclude so `True`/`False`
    # don't sneak through as numeric coordinates.
    if (
        not isinstance(lat, (int, float))
        or isinstance(lat, bool)
        or not isinstance(lng, (int, float))
        or isinstance(lng, bool)
        or not isinstance(captured, str)
    ):
        return None
    if not (-90 <= float(lat) <= 90) or not (-180 <= float(lng) <= 180):
        return None
    # `captured_at` is documented as ISO-8601 UTC; parse to confirm.
    # `fromisoformat` accepts both `+00:00` and (since Python 3.11)
    # trailing `Z`; we normalise the latter to keep older runtimes
    # working. Anything that doesn't resolve to a UTC instant is
    # rejected — a non-UTC zone would silently shift the freshness
    # window the caller computes against `now_utc`.
    try:
        parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return {"latitude": float(lat), "longitude": float(lng), "captured_at": captured}


_CONFIG_OPTIONAL_FIELDS: dict[str, type] = {
    "home_address": str,
    "min_transfer_minutes": int,
}


def write_config(config: dict) -> None:
    """Persist the tile-wide config. `schema_version` is set automatically.

    Validates field types and rejects undocumented keys per the
    writer/reader contract in `state-schema.md`. The optional fields
    today are: `home_address` (str) and `min_transfer_minutes` (int,
    non-negative). Add new fields here when bumping the config schema;
    same place as the schema doc.

    Raises ValueError on:
    - Wrong type for a documented field (incl. `bool` rejected when an
      int field is expected — `bool` is an `int` subclass in Python)
    - `min_transfer_minutes` below zero
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
        # Key-specific range checks. `min_transfer_minutes` must be >= 0
        # to match `_resolve_min_transfer_minutes` (precheck) and
        # `detect_connection_risks` (public API), which both reject
        # negatives. A negative value silently persisted here would
        # surface as a fallback / ValueError downstream — louder to
        # reject at the write surface.
        if key == "min_transfer_minutes" and value < 0:
            raise ValueError(
                f"write_config: field 'min_transfer_minutes' is {value}, "
                f"expected non-negative int"
            )
    payload = {**config, "schema_version": STATE_SCHEMA_VERSION}
    _atomic_write_json(state_dir() / CONFIG_FILE, payload)


def read_active_flights() -> list[int]:
    """Return the list of currently-tracked flight_ids. Empty list if no index.

    Owner-skill path: triggers `_migrate` on schema_version mismatch and
    rewrites the file at the latest version. Non-owner reader skills
    (e.g. sync-tripit) MUST call `read_active_flights_snapshot` instead
    — see `coding-policy: stateful-artifacts`.

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


def read_active_flights_snapshot() -> list[int]:
    """Non-owner reader entry point for the active-flights index.

    Same return shape as `read_active_flights`, but a schema_version
    strictly LESS THAN `STATE_SCHEMA_VERSION` is treated as "no usable
    prior state" (returns `[]`) instead of invoking `_migrate`. Per
    `coding-policy: stateful-artifacts`, only the owner skill
    (flight-assist) may migrate; non-owner skills (sync-tripit, other
    tiles) call this function on every read so they never trigger an
    owner-side rewrite.

    StateError is still raised on integrity failures: JSON corruption,
    non-object payload, missing schema_version, schema_version of a
    non-int type, or schema_version HIGHER than current
    `STATE_SCHEMA_VERSION` (forward incompatibility — operators must
    upgrade the consumer tile). The snapshot reader's "no usable prior
    state" semantics apply ONLY to older versions.
    """
    payload = _read_json_with_version(state_dir() / ACTIVE_FLIGHTS_FILE, migrate=False)
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

    Owner-skill path: triggers `_migrate` on schema_version mismatch.
    Non-owner reader skills MUST call `read_flight_state_snapshot`
    instead — see `coding-policy: stateful-artifacts`.

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


def read_flight_state_snapshot(flight_id: int) -> dict | None:
    """Non-owner reader entry point for per-flight state.

    Same shape as `read_flight_state`, but a schema_version strictly
    LESS THAN `STATE_SCHEMA_VERSION` is treated as "no usable prior
    state" (returns `None`) instead of invoking `_migrate`. Per
    `coding-policy: stateful-artifacts`, only the owner skill
    (flight-assist) may migrate; non-owner skills call this so they
    never trigger an owner-side rewrite. The next owner-skill
    invocation upgrades the file on its own read.

    StateError still raises on integrity failures: corrupt JSON,
    missing required field at the current schema, schema_version
    HIGHER than `STATE_SCHEMA_VERSION` (forward incompatibility —
    operators must upgrade the consumer tile). The snapshot reader's
    "no usable prior state" semantics apply ONLY to older versions.
    """
    _validate_flight_id(flight_id, fn_name="read_flight_state_snapshot")
    payload = _read_json_with_version(_flight_file(flight_id), migrate=False)
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
    # calendar_events: object keyed by event kind ("boarding", "flight")
    # → tracking entry for a flight-assist-owned/adopted Google Calendar
    # event. Validated structurally here (dict) only; the per-entry
    # shape is owned and deep-validated by the calendar-reconcile
    # planner — the same split as last_snapshot ↔ byair_client.get_flight.
    "calendar_events": (dict,),
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
