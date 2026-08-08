"""Persist the drive-or-fly verdict for a flight-less trip — the verdict store.

A trip booked with lodging and no flight is either a drive the engine should
plan the getting-there legs for, or a flight the operator has not booked yet.
The computed home→lodging drive time answers that on its own at the extremes;
in the middle band only the operator knows, so the engine asks once. This
module is the on-disk store of both answers.

Persisting is what makes the ask survivable: re-asking "drive or fly?" about
the same trip every ~30-minute sweep is the trust-eroding nag `skip_state`
exists to prevent for meetings, and the same rule applies here.

Verdicts expire. A verdict is meaningless once its trip is over, so the writer
sets each one's expiry past the trip's end; `load_verdicts` drops it after
that. Expiry is also the safety valve against a stale verdict suppressing a
real booking gap forever.

State file (per `coding-policy: stateful-artifacts`; see `state-schema.md`):
    <state_dir>/drive-decisions.json

Owner / contract:
    This module owns the SHAPE — only it migrates `schema_version`. Writers are
    co-bundled and go through the owner API: `reconcile_sweep.py` upserts the
    drive-time-derived verdict via `record_drive_time` and marks the question
    sent via `mark_asked`; `answer_drive_or_fly.py` writes the operator's answer
    via `record_operator_answer`. `check-travel-bookings` is a READ-ONLY,
    non-migrating consumer of the same file — it surfaces the missing-flight
    booking gap for a `fly` verdict and treats an absent, unreadable, or
    unrecognized-version file as "no verdict", never as a gap.

    An operator answer OUTRANKS the drive-time band: `record_drive_time` never
    overwrites a verdict `decided_by` the operator, so a sweep landing after the
    answer cannot revert it and re-ask.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from drive_decision import (
        VERDICT_DRIVE, VERDICT_FLY, VERDICT_UNKNOWN,
        load_verdicts, record_drive_time, record_operator_answer, mark_asked,
    )

    verdicts = load_verdicts(now)                       # → {trip_key: TripVerdict}
    record_drive_time(key, drive_seconds=13200, verdict=VERDICT_UNKNOWN,
                      expires=trip_end, now=now)
    mark_asked(key, now=now)                            # question sent
    record_operator_answer(key, VERDICT_DRIVE, now=now) # operator replied
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from skip_state import state_dir

DECISION_SCHEMA_VERSION = 1

_DECISION_FILE = "drive-decisions.json"

# The trip is a drive — plan the getting-there legs.
VERDICT_DRIVE = "drive"
# The trip is a flight — plan no drive; a missing flight is a booking gap.
VERDICT_FLY = "fly"
# The drive time lands in the ambiguous band and the operator has not answered.
VERDICT_UNKNOWN = "unknown"

_VERDICTS = frozenset({VERDICT_DRIVE, VERDICT_FLY, VERDICT_UNKNOWN})
# Answers the operator can give. `unknown` is a computed state, never an answer.
_OPERATOR_VERDICTS = frozenset({VERDICT_DRIVE, VERDICT_FLY})

DECIDED_BY_DRIVE_TIME = "drive_time"
DECIDED_BY_OPERATOR = "operator"


class DriveDecisionError(ValueError):
    """Raised on a malformed verdict file or a bad argument the caller must fix.

    A ValueError subclass — the fix is "pass a tz-aware datetime / repair the
    state file", not "retry". A *missing* file is never an error (it is
    indistinguishable from "no verdicts yet"); only a present-but-corrupt file
    or a future `schema_version` raises.
    """


@dataclass(frozen=True)
class TripVerdict:
    """One trip's drive-or-fly state.

    verdict — `drive`, `fly`, or `unknown` (asked or not yet asked).
    decided_by — `drive_time` when the band settled it, `operator` when they answered.
    drive_seconds — the routed home→lodging drive the band was read from.
    asked_at — when the question went out, or None while it has not been asked.
    expires — when this verdict stops applying (past the trip's end).
    """

    verdict: str
    decided_by: str
    drive_seconds: int | None
    asked_at: datetime | None
    expires: datetime

    @property
    def needs_question(self) -> bool:
        """Whether the operator still owes an answer — ambiguous and unasked."""
        return self.verdict == VERDICT_UNKNOWN and self.asked_at is None

    @property
    def is_operator_answer(self) -> bool:
        return self.decided_by == DECIDED_BY_OPERATOR


def _decision_path() -> Path:
    """The verdict file, in the store `skip_state` already owns the directory of."""
    return state_dir() / _DECISION_FILE


def _require_aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DriveDecisionError(f"`{name}` must be a timezone-aware datetime (got {value!r})")
    return value


def _require_key(key: object) -> str:
    if not isinstance(key, str) or not key:
        raise DriveDecisionError(f"`trip_key` must be a non-empty string (got {key!r})")
    return key


def _require_verdict(verdict: object, allowed: frozenset[str]) -> str:
    if verdict not in allowed:
        raise DriveDecisionError(f"`verdict` must be one of {sorted(allowed)} (got {verdict!r})")
    assert isinstance(verdict, str)
    return verdict


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
        # interrupt) a partial temp file is left behind — remove it so a crash
        # mid-write never strands a `.tmp` beside the real file.
        if os.path.exists(tmp):
            os.unlink(tmp)


def _parse_when(raw: object) -> datetime | None:
    """An ISO instant from the file as a tz-aware datetime, else None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _read_raw() -> dict[str, dict]:
    """Read the raw `{trip_key: record}` map from disk, validating the schema.

    A missing file returns an empty map. A *corrupt* file — unparseable, not an
    object, or missing/invalid `schema_version` — raises; treating it as "no
    verdicts" would drop every recorded answer and make the next sweep re-ask
    about every ambiguous trip, the nag `stateful-artifacts` forbids a fallback
    from escalating into.

    Schema version handling mirrors `skip_state._read_skips`: newer than this
    plugin raises on both read and write (reading it as empty loses answers; a
    write would clobber a newer writer's file), and anything below the current
    floor is corrupt rather than migratable — v1 is the first version. A future
    bump adds the v(N-1)→vN upgrade here instead of refusing.
    """
    path = _decision_path()
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise DriveDecisionError(
            f"drive-decision file {path} is unreadable / not valid JSON ({exc}) — "
            "repair or delete it"
        ) from exc

    if not isinstance(payload, dict):
        raise DriveDecisionError(f"drive-decision file {path} must contain a JSON object")

    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise DriveDecisionError(
            f"drive-decision file {path} is missing a valid integer schema_version"
        )
    if version > DECISION_SCHEMA_VERSION:
        raise DriveDecisionError(
            f"drive-decision file {path} has schema_version {version}, newer than this "
            f"plugin supports ({DECISION_SCHEMA_VERSION}) — upgrade the nanoclaw-travel "
            "plugin; refusing to read it as empty (that would drop every recorded "
            "drive-or-fly answer and re-ask about trips already settled) or to "
            "overwrite it as v1"
        )
    if version < DECISION_SCHEMA_VERSION:
        raise DriveDecisionError(
            f"drive-decision file {path} has schema_version {version}, below the current "
            f"floor ({DECISION_SCHEMA_VERSION}) with no migration path — repair or delete it"
        )

    trips = payload.get("trips")
    if not isinstance(trips, dict):
        raise DriveDecisionError(f"drive-decision file {path} `trips` must be a JSON object")
    return {
        key: record
        for key, record in trips.items()
        if isinstance(key, str) and key and isinstance(record, dict)
    }


def _to_verdict(record: dict) -> TripVerdict | None:
    """One raw record as a `TripVerdict`, or None when it is unusable.

    A malformed entry is dropped rather than raised on: unlike a corrupt FILE
    (which means every answer is in doubt), a single bad record just means that
    one trip has no verdict — the engine re-derives it from the drive time on
    this sweep and rewrites it.
    """
    expires = _parse_when(record.get("expires"))
    if expires is None:
        return None
    verdict = record.get("verdict")
    if verdict not in _VERDICTS:
        return None
    decided_by = record.get("decided_by")
    if decided_by not in (DECIDED_BY_DRIVE_TIME, DECIDED_BY_OPERATOR):
        return None
    drive_seconds = record.get("drive_seconds")
    if not isinstance(drive_seconds, int) or isinstance(drive_seconds, bool):
        drive_seconds = None
    return TripVerdict(
        verdict=verdict,
        decided_by=decided_by,
        drive_seconds=drive_seconds,
        asked_at=_parse_when(record.get("asked_at")),
        expires=expires,
    )


def _from_verdict(verdict: TripVerdict) -> dict:
    return {
        "verdict": verdict.verdict,
        "decided_by": verdict.decided_by,
        "drive_seconds": verdict.drive_seconds,
        "asked_at": verdict.asked_at.isoformat() if verdict.asked_at is not None else None,
        "expires": verdict.expires.isoformat(),
    }


def load_verdicts(now: datetime) -> dict[str, TripVerdict]:
    """Every still-active verdict at `now`, keyed by `travel-core`'s `trip_key`.

    Expired and malformed records are dropped from the returned map (the file
    itself is only rewritten by a write call — a read never mutates it).
    """
    _require_aware(now, "now")
    active: dict[str, TripVerdict] = {}
    for key, record in _read_raw().items():
        parsed = _to_verdict(record)
        if parsed is not None and parsed.expires > now:
            active[key] = parsed
    return active


def _save(key: str, verdict: TripVerdict) -> None:
    raw = _read_raw()
    raw[key] = _from_verdict(verdict)
    _atomic_write(_decision_path(), {"schema_version": DECISION_SCHEMA_VERSION, "trips": raw})


def record_drive_time(
    trip_key: str,
    *,
    verdict: str,
    drive_seconds: int,
    expires: datetime,
    now: datetime,
) -> TripVerdict:
    """Upsert the drive-time-derived verdict for a trip, preserving an answer.

    Returns the verdict now in force — the operator's when they have already
    answered (this call is then a no-op), otherwise the one just written. The
    preserved answer is why a sweep landing after the reply cannot revert it.

    `expires` should sit past the trip's end; `drive_seconds` is the routed
    home→lodging drive the band was read from, kept for the operator-facing
    question and for the booking-gap consumer.
    """
    key = _require_key(trip_key)
    _require_verdict(verdict, _VERDICTS)
    _require_aware(expires, "expires")
    _require_aware(now, "now")
    if not isinstance(drive_seconds, int) or isinstance(drive_seconds, bool):
        raise DriveDecisionError(f"`drive_seconds` must be an int (got {drive_seconds!r})")

    existing = _to_verdict(_read_raw().get(key, {}))
    if existing is not None and existing.is_operator_answer and existing.expires > now:
        return existing

    updated = TripVerdict(
        verdict=verdict,
        decided_by=DECIDED_BY_DRIVE_TIME,
        drive_seconds=drive_seconds,
        # Keep the asked stamp across re-derivations so an unanswered question
        # is asked once, not once per sweep.
        asked_at=existing.asked_at if existing is not None else None,
        expires=expires,
    )
    _save(key, updated)
    return updated


def mark_asked(trip_key: str, *, now: datetime) -> None:
    """Stamp that the drive-or-fly question has been sent for this trip.

    Raises when no verdict exists yet — the sweep records the drive time before
    it asks, so a missing record means the caller skipped a step rather than
    that the trip is new.
    """
    key = _require_key(trip_key)
    _require_aware(now, "now")
    existing = _to_verdict(_read_raw().get(key, {}))
    if existing is None:
        raise DriveDecisionError(
            f"no drive-decision record for {key!r} to mark asked — call `record_drive_time` first"
        )
    if existing.asked_at is not None:
        return
    _save(
        key,
        TripVerdict(
            verdict=existing.verdict,
            decided_by=existing.decided_by,
            drive_seconds=existing.drive_seconds,
            asked_at=now,
            expires=existing.expires,
        ),
    )


def record_operator_answer(
    trip_key: str,
    answer: str,
    *,
    now: datetime,
    expires: datetime | None = None,
) -> TripVerdict:
    """Record the operator's `drive` / `fly` answer, outranking the drive band.

    `expires` defaults to the existing record's expiry; it is required when no
    record exists (an answer to a question about a trip the store has since
    pruned). Returns the stored verdict.
    """
    key = _require_key(trip_key)
    _require_verdict(answer, _OPERATOR_VERDICTS)
    _require_aware(now, "now")
    existing = _to_verdict(_read_raw().get(key, {}))
    if expires is None:
        if existing is None:
            raise DriveDecisionError(
                f"no drive-decision record for {key!r} and no `expires` given — "
                "pass the trip's expiry to record an answer for an unknown trip"
            )
        expires = existing.expires
    _require_aware(expires, "expires")

    updated = TripVerdict(
        verdict=answer,
        decided_by=DECIDED_BY_OPERATOR,
        drive_seconds=existing.drive_seconds if existing is not None else None,
        asked_at=existing.asked_at if existing is not None else None,
        expires=expires,
    )
    _save(key, updated)
    return updated


def prune(now: datetime) -> int:
    """Drop expired and malformed records from the file. Returns how many went.

    Idempotent: pruning an already-clean store rewrites nothing.
    """
    _require_aware(now, "now")
    raw = _read_raw()
    kept: dict[str, dict] = {}
    for key, record in raw.items():
        parsed = _to_verdict(record)
        if parsed is not None and parsed.expires > now:
            kept[key] = record
    dropped = len(raw) - len(kept)
    if dropped:
        _atomic_write(_decision_path(), {"schema_version": DECISION_SCHEMA_VERSION, "trips": kept})
    return dropped
