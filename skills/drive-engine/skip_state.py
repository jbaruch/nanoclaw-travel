"""Persist user "skip this meeting" decisions with auto-expiry — the skip store.

When drive-engine asks "drive or skip?" and the user says skip, that answer
has to stick: re-asking about the same skipped meeting every sweep is the
trust-eroding nag LoMBot hit (Epic #59 §5 #49). This module is the on-disk
store of those skips. `scan.py` consumes the `{meeting_id: expiry}` mapping
this module returns and buckets a still-active skip as `skipped` instead of
`needs_decision`.

Skips expire. A skip is meaningless once its meeting is over, so the writer
sets each skip's expiry to the meeting's end; once that passes,
`load_active_skips` drops it and the id would re-enter `needs_decision` if it
ever recurred. Expiry is also the safety valve against a skip file that
silently suppresses a meeting forever.

State file (per `coding-policy: stateful-artifacts`; see `state-schema.md`):
    <state_dir>/skip-state.json
    {"schema_version": 1, "skips": {"<meeting_id>": "<ISO-8601 expiry>"}}

Owner / contract:
    This module owns the SHAPE — only it migrates `schema_version`. Its writer
    and reader are both co-bundled: `skip_drive.py` WRITES via `add_skip` /
    `clear_skip` / `prune`, and `reconcile_sweep.py` READS via
    `load_active_skips`, feeding the result to `scan(skip_state=...)`. The
    drive-planner sweep that used to write is retired (#156) and its bundle is
    folded into this one (#181).

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from skip_state import add_skip, load_active_skips, clear_skip, prune

    add_skip("evt_42", expires=meeting_end, now=now)   # user said skip
    active = load_active_skips(now)                     # → {"evt_42": "..."}
    scan(events, now=now, home_address=home, skip_state=active)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

SKIP_SCHEMA_VERSION = 1

_DEFAULT_STATE_DIR = "/workspace/state/drive-planner"
_STATE_DIR_ENV = "DRIVE_PLANNER_STATE_DIR"
_SKIP_FILE = "skip-state.json"


class SkipStateError(ValueError):
    """Raised on a malformed skip-state file or a bad argument the caller must fix.

    A ValueError subclass — the fix is "pass a tz-aware datetime / repair the
    state file", not "retry". A *missing* file is never an error (it is
    indistinguishable from "no skips yet"); only a present-but-corrupt file
    or a future `schema_version` raises.
    """


def state_dir() -> Path:
    """The skip-store state directory.

    Defaults to `/workspace/state/drive-planner`; overridable via the
    `DRIVE_PLANNER_STATE_DIR` env var (tests point it at a tmp_path). The
    directory is created on first write, not here.

    The `drive-planner` name in both is deployed state, not a live reference:
    the store predates the #181 fold into drive-engine and renaming either
    would strand the skips already on disk. Rename only behind a migration.
    """
    return Path(os.environ.get(_STATE_DIR_ENV, _DEFAULT_STATE_DIR))


def _skip_path() -> Path:
    return state_dir() / _SKIP_FILE


def _require_aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SkipStateError(f"`{name}` must be a timezone-aware datetime (got {value!r})")
    return value


def _require_meeting_id(meeting_id: object) -> str:
    if not isinstance(meeting_id, str) or not meeting_id:
        raise SkipStateError(f"`meeting_id` must be a non-empty string (got {meeting_id!r})")
    return meeting_id


def _atomic_write(path: Path, payload: dict) -> None:
    """Write JSON to `path` atomically (temp file + rename) so a crash mid-write
    never leaves a half-written, unparseable state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        # On success the rename consumed `tmp`; on any failure (including an
        # interrupt) a partial temp file is left behind — remove it so a
        # crash mid-write never strands a `.tmp` beside the real file.
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_skips(*, for_write: bool) -> dict[str, str]:
    """Read the skip map from disk, validating the schema.

    Returns the raw `{meeting_id: expiry}` mapping (no pruning). A missing
    file returns an empty map. A *corrupt* file — unparseable, not an object,
    or missing/invalid `schema_version` — raises SkipStateError; a corrupt
    skip file must not be silently treated as "no skips" (that would
    resurrect every skipped meeting as a nag).

    Schema version handling (per `coding-policy: stateful-artifacts`):
      - newer than this plugin, `for_write=False` (read-only) → "no usable
        prior state": return an empty map. The reader is lagging, not
        awaiting migration; an empty map is the safe, non-disruptive
        fallback (worst case the sweep re-asks — it never escalates work).
      - newer than this plugin, `for_write=True` → raise. The no-prior-state
        fallback is read-only; a write that proceeded would rewrite the
        future-version file as v1 and clobber a newer writer's state. The
        write path refuses instead of downgrading.
      - below the current floor → owner-side migration point. v1 is the
        first and only version, so any lower value is corrupt, not an older
        record to migrate — refuse explicitly. A future bump adds the
        v(N-1)→vN upgrade-and-rewrite here instead of refusing.
    """
    path = _skip_path()
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SkipStateError(
            f"skip-state file {path} is unreadable / not valid JSON ({exc}) — repair or delete it"
        ) from exc

    if not isinstance(payload, dict):
        raise SkipStateError(f"skip-state file {path} must contain a JSON object")

    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise SkipStateError(f"skip-state file {path} is missing a valid integer schema_version")
    if version > SKIP_SCHEMA_VERSION:
        if for_write:
            raise SkipStateError(
                f"skip-state file {path} has schema_version {version}, newer than this "
                f"plugin supports ({SKIP_SCHEMA_VERSION}) — refusing to overwrite it; "
                "upgrade the nanoclaw-travel plugin before writing"
            )
        return {}
    if version < SKIP_SCHEMA_VERSION:
        raise SkipStateError(
            f"skip-state file {path} has schema_version {version}, below the current "
            f"floor ({SKIP_SCHEMA_VERSION}) with no migration path — repair or delete it"
        )

    skips = payload.get("skips")
    if not isinstance(skips, dict):
        raise SkipStateError(f"skip-state file {path} `skips` must be a JSON object")
    # Coerce to the {str: str} contract; drop any malformed entry rather than
    # let it crash a downstream expiry parse.
    return {
        str(mid): expiry
        for mid, expiry in skips.items()
        if isinstance(mid, str) and isinstance(expiry, str)
    }


def _write_skips(skips: dict[str, str]) -> None:
    _atomic_write(_skip_path(), {"schema_version": SKIP_SCHEMA_VERSION, "skips": skips})


def _is_active(expiry_iso: str, now: datetime) -> bool:
    """True when `expiry_iso` parses to a tz-aware time strictly after `now`.

    A malformed or naive expiry is treated as inactive (expired) — an
    unusable expiry must not pin a skip forever.
    """
    text = expiry_iso.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        expiry = datetime.fromisoformat(text)
    except ValueError:
        return False
    if expiry.tzinfo is None:
        return False
    return expiry > now


def add_skip(meeting_id: str, *, expires: datetime, now: datetime) -> None:
    """Record a skip for `meeting_id`, expiring at `expires`.

    Idempotent — re-skipping the same meeting updates its expiry. Expired
    entries are pruned in the same write, so the file does not grow without
    bound.

    Raises:
        SkipStateError: on an empty meeting_id or a naive `expires` / `now`.
    """
    meeting_id = _require_meeting_id(meeting_id)
    expires = _require_aware(expires, "expires")
    now = _require_aware(now, "now")

    skips = {mid: exp for mid, exp in _read_skips(for_write=True).items() if _is_active(exp, now)}
    skips[meeting_id] = expires.isoformat()
    _write_skips(skips)


def load_active_skips(now: datetime) -> dict[str, str]:
    """Return the `{meeting_id: expiry}` mapping of skips still active at `now`.

    This is the shape `scan(skip_state=...)` consumes. Read-only — it filters
    expired entries out of the returned mapping but does not rewrite the file
    (call `prune` to reclaim disk). A missing file returns an empty mapping.

    Raises:
        SkipStateError: on a naive `now` or a corrupt skip file.
    """
    now = _require_aware(now, "now")
    return {mid: exp for mid, exp in _read_skips(for_write=False).items() if _is_active(exp, now)}


def clear_skip(meeting_id: str, *, now: datetime) -> bool:
    """Remove a skip for `meeting_id`. Returns True if one was present.

    Also prunes expired entries in the rewrite. A no-op (id absent) does not
    rewrite the file.

    Raises:
        SkipStateError: on an empty meeting_id or a naive `now`.
    """
    meeting_id = _require_meeting_id(meeting_id)
    now = _require_aware(now, "now")

    current = _read_skips(for_write=True)
    if meeting_id not in current:
        return False
    survivors = {
        mid: exp for mid, exp in current.items() if mid != meeting_id and _is_active(exp, now)
    }
    _write_skips(survivors)
    return True


def prune(now: datetime) -> int:
    """Drop every expired skip and rewrite the file. Returns the count removed.

    A no-op (nothing expired, or no file) does not rewrite.

    Raises:
        SkipStateError: on a naive `now` or a corrupt skip file.
    """
    now = _require_aware(now, "now")
    current = _read_skips(for_write=True)
    survivors = {mid: exp for mid, exp in current.items() if _is_active(exp, now)}
    removed = len(current) - len(survivors)
    if removed:
        _write_skips(survivors)
    return removed
