"""Per-flight state file read/write for the flight-assist plugin.

The precheck script reads + writes state across invocations to detect
deltas between byAir snapshots. State lives under
`/workspace/state/flight-assist/` in production; tests override the
directory via the `FLIGHT_ASSIST_STATE_DIR` environment variable.

Files written (all JSON, all carry `schema_version: 6` at the top level):

    config.json                       — home_address, etc. (set via /setup)
    active-flights.json               — list of currently-tracked flight_ids
    flight-<flight_id>.json           — per-flight state record

Writes are atomic: write-to-tmp + os.replace, so a kill mid-write
doesn't leave a half-written file on disk.

Owner skill: `flight-assist` (this plugin). Per `coding-policy:
stateful-artifacts`, only the owner skill migrates `schema_version`.
Reader skills (other plugins, future agent-side actions) treat any
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
        list_flight_state_ids,
        # Non-owner reader entry points (snapshot semantics — never
        # invoke _migrate, so calling plugins do not rewrite owner state):
        read_active_flights_snapshot,
        read_flight_state_snapshot,
        state_dir,
    )

Non-owner reader contract: any plugin that reads (but does not own) this
state — sync-tripit, future agent-side composition, other plugins — MUST
use the `*_snapshot` entry points. They treat a schema_version BELOW
the current `STATE_SCHEMA_VERSION` as "no usable prior state" (return
None / []) without rewriting the file, satisfying `coding-policy:
stateful-artifacts`'s single-owner migration rule. A schema_version
ABOVE the current still raises `StateError` (forward incompatibility,
not an old-state case) — operators need to upgrade the consumer plugin,
not be told there's nothing on disk.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_SCHEMA_VERSION = 6

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


def _type_name(expected: type | tuple[type, ...]) -> str:
    """Readable name for an expected type — a single type or a tuple of them.

    `int` → "int"; `(str, type(None))` → "str or NoneType". The schema dicts
    permit tuple types (type-or-None fields), so formatting an error message
    with a bare `expected.__name__` would itself raise `AttributeError` on the
    tuple — masking the real validation error.
    """
    if isinstance(expected, tuple):
        return " or ".join(member.__name__ for member in expected)
    return expected.__name__


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
            f"{fn_name}: flight_id must be int, got {type(flight_id).__name__} {flight_id!r}"
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
    migrates; readers from other plugins get `StateError` on mismatch.

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
        # v2 → v3: per-flight records (flight-<id>.json) gain an empty
        # `calendar_events` map (flight-assist-owned/adopted Google
        # Calendar event IDs — see state-schema.md). Scoped by filename,
        # not payload contents: a config/active-flights file — or any
        # future record that happens to carry a `flight_id` key —
        # must never be given this per-flight-only field. Config and
        # active-flights only get a schema_version bump at v3.
        is_flight_file = path.name.startswith(_FLIGHT_FILE_PREFIX) and path.name.endswith(
            _FLIGHT_FILE_SUFFIX
        )
        if is_flight_file and "calendar_events" not in payload:
            payload["calendar_events"] = {}
        version = 3
    if version == 3:
        # v3 → v4: config.json gains two optional calendar-reconcile fields
        # (`byair_calendar_name`, `byair_calendar_id` — see state-schema.md).
        # Both are optional and absent-tolerant, so there is no shape change
        # to apply on migration — config, active-flights, and per-flight
        # records only get a schema_version bump at v4.
        version = 4
    if version == 4:
        # v4 → v5: config.json gains five optional airport-clearance fields
        # (`airport_clearance_*`, `airport_post_arrival_*` — see
        # state-schema.md). All optional and absent-tolerant, so there is no
        # shape change to apply on migration — config, active-flights, and
        # per-flight records only get a schema_version bump at v5.
        version = 5
    if version == 5:
        # v5 → v6: add `gate_assignment_fired: False` to per-flight
        # phase_markers (the once-per-flight gate/terminal readout gate —
        # see state-schema.md, #103). Scoped by filename (matching v2→v3), not
        # payload contents: a config/active-flights file — or any future record
        # that happens to carry a `phase_markers` key — must never be mutated.
        # Config and active-flights only get a schema_version bump at v6.
        is_flight_file = path.name.startswith(_FLIGHT_FILE_PREFIX) and path.name.endswith(
            _FLIGHT_FILE_SUFFIX
        )
        phase_markers = payload.get("phase_markers")
        if (
            is_flight_file
            and isinstance(phase_markers, dict)
            and "gate_assignment_fired" not in phase_markers
        ):
            phase_markers["gate_assignment_fired"] = False
        version = 6
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
    """Return the plugin-wide config (home_address, etc.) or None if not set."""
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


# Maximum age of a `current-location.json` snapshot to be used as a drive
# origin. Older than this and origin resolution falls back to `home_address`,
# on the principle that a stale location guess is worse than the static home
# base (#18). 30 min matches the orchestrator's typical Telegram
# live-location write cadence.
MAX_LIVE_ORIGIN_AGE_MINUTES = 30


def resolve_live_origin(home_address: str | None, *, now: datetime) -> str | None:
    """Resolve the drive origin: fresh live location → `home_address` → None.

    The single origin-resolution ladder shared by the precheck's time-to-leave
    query and the airport-drive reconcile, so the two never disagree on where the
    user is:

    1. `current-location.json` (orchestrator-written) when present and fresh —
       `0 <= now - captured_at <= MAX_LIVE_ORIGIN_AGE_MINUTES`. Returned as
       `"<lat>,<lng>"`, which Distance Matrix accepts as a numeric origin.
    2. `home_address` — the caller-supplied fallback. Both production
       callers pass the trip-aware effective home (#122,
       `trip_origin.resolve_effective_home`): the config's static residence
       off-trip, the current lodging while a trip is active.
    3. `None` — neither available; the caller skips routing.

    `now` must be timezone-aware (UTC) — a `now` with no usable offset (no
    `tzinfo`, or a `tzinfo` whose `utcoffset()` is None) is rejected up front with
    a clear `ValueError` rather than failing mid-subtraction against the aware
    `captured_at`. `read_current_location` already shape-validates the snapshot
    and returns None on any mismatch (including a `captured_at` that does not
    resolve to a UTC instant), so a corrupt or stale-schema file falls through to
    `home_address` here, and the `captured_at` on a returned snapshot is always a
    parseable UTC string.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("resolve_live_origin: `now` must be timezone-aware (UTC)")
    loc = read_current_location()
    if loc is not None:
        captured = datetime.fromisoformat(loc["captured_at"].replace("Z", "+00:00"))
        age = now - captured
        if timedelta() <= age <= timedelta(minutes=MAX_LIVE_ORIGIN_AGE_MINUTES):
            return f"{loc['latitude']},{loc['longitude']}"
    return home_address


_CONFIG_OPTIONAL_FIELDS: dict[str, type] = {
    "home_address": str,
    "min_transfer_minutes": int,
    # Calendar reconciliation (#55). The flight ("Flighty Flights") calendar
    # the reconcile script reads/writes is resolved at runtime — never
    # hardcoded in the plugin per `rules/flight-data-locality.md`. The operator
    # supplies its display name in `byair_calendar_name` (operator data, not
    # plugin code); the reconcile lists calendars, matches that name once, and
    # caches the resolved id in `byair_calendar_id` so later cycles skip the
    # lookup. The Reclaim travel blocks live on the primary calendar
    # (content-classified — there is no dedicated Reclaim calendar), so no
    # config field is needed for it.
    "byair_calendar_name": str,
    "byair_calendar_id": str,
    # Airport drive block clearance policy (#90). Operator risk-tolerance knobs
    # that override `airport_lead.py`'s defaults: how early to be at the airport
    # before departure (domestic / international), and how long after landing
    # before the drive home can start (domestic / international into the US /
    # international abroad). All minutes, all non-negative. Absent → the
    # `airport_lead` defaults apply. The byAir delay-index nudge (low/med/high)
    # stays an `airport_lead` constant — it is keyed on byAir's `delay.index`
    # and does not fit this flat int-field shape; make it configurable later if
    # needed.
    "airport_clearance_domestic_minutes": int,
    "airport_clearance_international_minutes": int,
    "airport_post_arrival_domestic_minutes": int,
    "airport_post_arrival_intl_us_minutes": int,
    "airport_post_arrival_intl_abroad_minutes": int,
}

# Optional int config fields that must be non-negative. A negative value
# persisted here would surface as a fallback / ValueError downstream, so it is
# rejected louder at the write surface (see `write_config`).
_CONFIG_NON_NEGATIVE_INT_FIELDS = frozenset(
    {
        "min_transfer_minutes",
        "airport_clearance_domestic_minutes",
        "airport_clearance_international_minutes",
        "airport_post_arrival_domestic_minutes",
        "airport_post_arrival_intl_us_minutes",
        "airport_post_arrival_intl_abroad_minutes",
    }
)


def write_config(config: dict) -> None:
    """Persist the plugin-wide config. `schema_version` is set automatically.

    Validates field types and rejects undocumented keys per the
    writer/reader contract in `state-schema.md`. The accepted optional
    fields are `_CONFIG_OPTIONAL_FIELDS` (`home_address` and the calendar /
    airport-clearance fields); the int fields that must be non-negative are
    `_CONFIG_NON_NEGATIVE_INT_FIELDS`. Add new fields to those when bumping
    the config schema; same place as the schema doc.

    Raises ValueError on:
    - Wrong type for a documented field (incl. `bool` rejected when an
      int field is expected — `bool` is an `int` subclass in Python)
    - A `_CONFIG_NON_NEGATIVE_INT_FIELDS` field below zero
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
                f"{value!r}, expected {_type_name(expected_type)}"
            )
        # Key-specific range checks. `min_transfer_minutes` must be >= 0
        # to match `_resolve_min_transfer_minutes` (precheck) and
        # `detect_connection_risks` (public API), which both reject
        # negatives. A negative value silently persisted here would
        # surface as a fallback / ValueError downstream — louder to
        # reject at the write surface.
        if key in _CONFIG_NON_NEGATIVE_INT_FIELDS and value < 0:
            raise ValueError(f"write_config: field '{key}' is {value}, expected non-negative int")
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
    plugins) call this function on every read so they never trigger an
    owner-side rewrite.

    StateError is still raised on integrity failures: JSON corruption,
    non-object payload, missing schema_version, schema_version of a
    non-int type, or schema_version HIGHER than current
    `STATE_SCHEMA_VERSION` (forward incompatibility — operators must
    upgrade the consumer plugin). The snapshot reader's "no usable prior
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
            f"write_active_flights: flight_ids must be a list — got {type(flight_ids).__name__}"
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
    operators must upgrade the consumer plugin). The snapshot reader's
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
                f"{type(value).__name__} {value!r}, expected {_type_name(expected_type)}"
            )

    # Phase markers structurally validated on read too, not just on write,
    # so a hand-edited file with `phase_markers: {}` raises rather than
    # silently passing through.
    try:
        _validate_phase_markers(payload["phase_markers"])
    except ValueError as marker_err:
        raise StateError(
            f"flight state file {source}: {marker_err} — remove or restore the file"
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

    Optional keys, type-checked against the documented shape when
    present (readers must tolerate them missing):

        last_snapshot (object or null), last_wake_at (str or null),
        last_wake_reason (str or null), calendar_events (object)

    `calendar_events` is validated structurally (object) only; its
    per-entry shape is owned by the calendar-reconcile planner, the
    same split as last_snapshot ↔ byair_client.

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
                f"{type(value).__name__} {value!r}, expected {_type_name(expected_type)}"
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
        "gate_assignment_fired",
    }
)


def _validate_phase_markers(phase_markers: dict) -> None:
    """Verify phase_markers has exactly the 7 documented keys, all bool.

    Per state-schema.md: phase_markers is `{day_before_fired,
    time_to_leave_fired, boarding_fired, arrival_logistics_fired,
    landed_acknowledged, connection_at_risk_fired, gate_assignment_fired}` —
    each a plain `bool`. No undocumented keys, no missing keys, no non-bool
    values.
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


def list_flight_state_ids() -> list[int]:
    """Return the flight_ids of every `flight-<id>.json` on disk, sorted.

    Enumerates the per-flight state files regardless of active-flights
    membership. The calendar-teardown sweep
    (`calendar_reconcile.run_reconcile`) needs to see flights that have
    dropped out of `active-flights.json` but still carry a `calendar_events`
    tombstone — the per-flight wake loop only visits active flights, so
    enumerating the index would miss exactly the switched-away flights the
    sweep exists to clean up. Returns `[]` when the state directory does not
    exist yet (first run, before any write).

    Non-`flight-<id>.json` files (`config.json`, `active-flights.json`,
    `current-location.json`) and any `flight-*.json` whose middle segment is
    not a plain integer are skipped.
    """
    directory = state_dir()
    if not directory.is_dir():
        return []
    ids: list[int] = []
    for path in directory.iterdir():
        name = path.name
        if not (name.startswith(_FLIGHT_FILE_PREFIX) and name.endswith(_FLIGHT_FILE_SUFFIX)):
            continue
        middle = name[len(_FLIGHT_FILE_PREFIX) : -len(_FLIGHT_FILE_SUFFIX)]
        try:
            ids.append(int(middle))
        except ValueError:
            continue
    return sorted(ids)
