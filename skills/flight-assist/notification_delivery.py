"""Durable flight notice attempts and confirmed deliveries.

The flight-assist owner alone writes notifications.json (schema 1). Its file
lock spans a caller's read/modify/write cycle. Offering a notice persists it
before the flight snapshot may advance; offering is never a delivery receipt.
A due notice gets a retry deadline, and only confirm() consumes its exact id.
A changed pending payload gets a new id, so an old acknowledgement cannot
consume the replacement. Historical phase flags are not imported as receipts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from state import state_dir

SCHEMA_VERSION = 1
MAX_BYTES = 4 * 1024 * 1024
MAX_NOTICES = 2048
MAX_EVENT_BYTES = 8192
RETRY_SECONDS = 300
PHASE_BY_REASON = {
    "day_before": "day_before_fired",
    "time_to_leave": "time_to_leave_fired",
    "boarding_started": "boarding_fired",
    "arrival_logistics": "arrival_logistics_fired",
    "connection_at_risk": "connection_at_risk_fired",
    "gate_assignment": "gate_assignment_fired",
}
PHASE_MARKERS = frozenset(PHASE_BY_REASON.values())
_ROW_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "key",
        "sequence",
        "prior_poll",
        "flight_id",
        "event",
        "marker",
        "expires_at",
        "retry_at",
        "confirmed_at",
    }
)


class DeliveryError(Exception):
    """Restore the documented delivery store instead of clearing its receipts."""


class DeliveryBusy(DeliveryError):
    """Another owner invocation holds the store; retry without sending again."""


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Pass a timezone-aware notification clock.")
    return value.astimezone(UTC).isoformat()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Notification timestamps must carry a timezone.")
    return parsed.astimezone(UTC)


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"schema_version", "sequence", "notices"}:
        raise ValueError("Unsupported notification store shape.")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported notification store version.")
    if type(data["sequence"]) is not int or data["sequence"] < 0:
        raise ValueError("Invalid notification sequence.")
    if not isinstance(data["notices"], dict) or len(data["notices"]) > MAX_NOTICES:
        raise ValueError("Invalid notification collection.")
    ids = set()
    for key, row in data["notices"].items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("Invalid notification key.")
        if not isinstance(row, dict) or set(row) != _ROW_KEYS or row["key"] != key:
            raise ValueError("Invalid notification record.")
        if type(row["schema_version"]) is not int or row["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported notification record version.")
        if not isinstance(row["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", row["id"]):
            raise ValueError("Invalid notification id.")
        if row["id"] in ids:
            raise ValueError("Duplicate notification id.")
        ids.add(row["id"])
        if type(row["sequence"]) is not int or not 0 < row["sequence"] <= data["sequence"]:
            raise ValueError("Invalid notification record sequence.")
        if row["flight_id"] is not None and type(row["flight_id"]) is not int:
            raise ValueError("Invalid notification flight id.")
        if not isinstance(row["event"], dict) or not isinstance(row["event"].get("reason"), str):
            raise ValueError("Invalid notification event.")
        if len(_json(row["event"]).encode()) > MAX_EVENT_BYTES:
            raise ValueError("Oversized notification event.")
        if row["marker"] != PHASE_BY_REASON.get(row["event"]["reason"]):
            raise ValueError("Unsupported phase marker.")
        if row["prior_poll"] is not None and not isinstance(row["prior_poll"], str):
            raise ValueError("Invalid prior-poll identity.")
        identity_input = (
            [row["flight_id"], row["marker"]]
            if row["marker"]
            else [row["flight_id"], row["prior_poll"], row["event"]]
        )
        if (
            key != _digest(identity_input)
            or row["id"] != _digest([row["sequence"], key, row["event"]])[:32]
        ):
            raise ValueError("Notification identity differs from its stored payload.")
        _dependencies(row)
        for field in ("expires_at", "retry_at", "confirmed_at"):
            value = row[field]
            if value is None and field != "expires_at":
                continue
            if not isinstance(value, str):
                raise ValueError("Invalid notification timestamp.")
            _time(value)
    return data


def _dependencies(row: dict) -> set[int]:
    result = {row["flight_id"]} if row["flight_id"] is not None else set()
    if row["event"]["reason"] == "connection_at_risk":
        dependency = row["event"].get("leg1_flight_id")
        if type(dependency) is not int:
            raise ValueError("A connection notice must identify its inbound flight.")
        result.add(dependency)
    return result


class DeliveryStore:
    def __init__(self, path: Path, data: dict[str, Any], now_utc: datetime) -> None:
        self.path = path
        self.data = data
        self.now = _time(_stamp(now_utc))

    def _save(self, updated: dict[str, Any]) -> None:
        _validate(updated)
        raw = _json(updated).encode()
        if len(raw) > MAX_BYTES:
            raise DeliveryError(
                f"Notification store {self.path} is full; inspect pending notices before retrying."
            )
        fd, temporary = tempfile.mkstemp(prefix=".notifications-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.data = updated
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def offer(
        self,
        flight_id: int | None,
        event: dict,
        *,
        prior_poll: str | None,
        expires_at: datetime,
    ) -> str | None:
        """Persist one current observation; None means already confirmed or expired."""
        expiry = _time(_stamp(expires_at))
        if expiry <= self.now:
            return None
        event = json.loads(_json(event))
        reason = event.get("reason")
        if not isinstance(reason, str):
            raise ValueError("Supply a flight event with its reason.")
        marker = PHASE_BY_REASON.get(reason)
        key = _digest([flight_id, marker] if marker else [flight_id, prior_poll, event])
        previous = self.data["notices"].get(key)
        if previous and previous["confirmed_at"] is not None:
            return None
        if previous and previous["event"] == event and _time(previous["expires_at"]) > self.now:
            return previous["id"]
        if previous is None and len(self.data["notices"]) >= MAX_NOTICES:
            raise DeliveryError(
                f"Notification store {self.path} is full; inspect pending notices "
                "and the active-flight index before retrying; do not erase receipts."
            )
        updated = json.loads(_json(self.data))
        sequence = updated["sequence"] + 1
        identity = _digest([sequence, key, event])[:32]
        updated["sequence"] = sequence
        updated["notices"][key] = {
            "schema_version": SCHEMA_VERSION,
            "id": identity,
            "key": key,
            "sequence": sequence,
            "prior_poll": prior_poll,
            "flight_id": flight_id,
            "event": event,
            "marker": marker,
            "expires_at": _stamp(expiry),
            "retry_at": None,
            "confirmed_at": None,
        }
        self._save(updated)
        return identity

    def prune(self, *, active_ids: set[int]) -> None:
        """Make room before observations; retain active flights' delivery receipts."""
        updated = json.loads(_json(self.data))
        for key, row in list(updated["notices"].items()):
            if (
                row["flight_id"] is not None
                and row["flight_id"] not in active_ids
                or row["confirmed_at"] is None
                and _time(row["expires_at"]) <= self.now
            ):
                del updated["notices"][key]
        if updated != self.data:
            self._save(updated)

    def retry_due_ids(self) -> set[int]:
        result: set[int] = set()
        for row in self.data["notices"].values():
            if (
                row["confirmed_at"] is None
                and _time(row["expires_at"]) > self.now
                and (row["retry_at"] is None or _time(row["retry_at"]) <= self.now)
            ):
                result.update(_dependencies(row))
        return result

    def reconcile(
        self,
        *,
        verified_ids: set[int],
        current_phases: set[tuple[int, str]],
        is_current: Callable[[int | None, dict], bool],
    ) -> None:
        """Discard obsolete pending observations only after their sources refreshed."""
        updated = json.loads(_json(self.data))
        for key, row in list(updated["notices"].items()):
            if row["confirmed_at"] is not None or not _dependencies(row).issubset(verified_ids):
                continue
            phase_current = (
                row["marker"] is None
                # Boarding is detected on transition; its retry stays current while
                # the live snapshot still satisfies the boarding predicate below.
                or row["marker"] == "boarding_fired"
                or (row["flight_id"], row["marker"]) in current_phases
            )
            if not phase_current or not is_current(row["flight_id"], row["event"]):
                del updated["notices"][key]
        if updated != self.data:
            self._save(updated)

    def due(
        self, *, active_ids: set[int], excluded_ids: set[int], removed_ids: set[int] | None = None
    ) -> list[dict]:
        """Record an attempt for eligible notices without recording delivery."""
        updated = json.loads(_json(self.data))
        ready = []
        for key, row in list(updated["notices"].items()):
            fid = row["flight_id"]
            if fid is not None and fid not in active_ids:
                del updated["notices"][key]
                continue
            if row["confirmed_at"] is not None:
                continue
            dependencies = _dependencies(row)
            if not dependencies.issubset(active_ids):
                del updated["notices"][key]
                continue
            if _time(row["expires_at"]) <= self.now:
                del updated["notices"][key]
                continue
            if (
                removed_ids
                and dependencies.intersection(removed_ids)
                and row["event"]["reason"] != "removed_upstream"
            ):
                del updated["notices"][key]
                continue
            if (
                dependencies.intersection(excluded_ids)
                or row["retry_at"]
                and _time(row["retry_at"]) > self.now
            ):
                continue
            row["retry_at"] = _stamp(self.now + timedelta(seconds=RETRY_SECONDS))
            ready.append(
                json.loads(
                    _json({"flight_id": fid, "event": row["event"], "notification_id": row["id"]})
                )
            )
        if updated != self.data:
            self._save(updated)
        return ready

    def confirm(self, identities: list[str]) -> dict:
        """Acknowledge only exact notices whose send was confirmed by the caller."""
        if (
            not identities
            or len(identities) > MAX_NOTICES
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value)
                for value in identities
            )
        ):
            raise ValueError("Supply the exact notification ids from confirmed sends.")
        updated = json.loads(_json(self.data))
        by_id = {row["id"]: row for row in updated["notices"].values()}
        confirmed, stale = [], []
        for identity in dict.fromkeys(identities):
            row = by_id.get(identity)
            if row is None:
                stale.append(identity)
            else:
                if row["confirmed_at"] is None:
                    row["confirmed_at"] = _stamp(self.now)
                confirmed.append(identity)
        if updated != self.data:
            self._save(updated)
        return {"confirmed": confirmed, "stale": stale}

    def confirmed_markers(self, flight_id: int) -> set[str]:
        return {
            row["marker"]
            for row in self.data["notices"].values()
            if row["flight_id"] == flight_id
            and row["confirmed_at"] is not None
            and row["marker"] is not None
        }


@contextmanager
def notification_store(*, now_utc: datetime) -> Iterator[DeliveryStore]:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "notifications.json"
    with (directory / "notifications.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeliveryBusy(
                "Flight notices are being updated; "
                "retry the receipt acknowledgement without resending."
            ) from error
        try:
            try:
                with path.open("rb") as source:
                    raw = source.read(MAX_BYTES + 1)
            except FileNotFoundError:
                data = {"schema_version": SCHEMA_VERSION, "sequence": 0, "notices": {}}
            else:
                try:
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Oversized notification store.")
                    data = _validate(json.loads(raw))
                except (ValueError, TypeError, KeyError, RecursionError) as error:
                    raise DeliveryError(
                        f"Restore the supported notification store at {path}; "
                        f"do not delete its delivery receipts: {error}"
                    ) from error
            yield DeliveryStore(path, data, now_utc)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
