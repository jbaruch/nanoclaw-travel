"""Owner of explicit conference-presence planner inputs; no provider operations.

Only grounded intake records labels/dates. Calendar text is compared literally,
never classified as attendance by a conference name or city substring.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from skip_state import state_dir  # noqa: E402

MAX_BYTES = 256 * 1024
_FILENAME = "conference-attendance.json"


class AttendanceError(ValueError):
    """Repair the attendance input or upgrade its owner; never treat it as empty."""


@dataclass(frozen=True)
class Attendance:
    id: str
    starts_on: str
    ends_on: str
    time_zone: str
    venue_locations: tuple[str, ...]

    def covers(self, identity: object, location: object, start: object, end: object) -> bool:
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or len(identity) > 1024
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in identity)
        ):
            return False
        if not isinstance(location, str):
            return False
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            return False
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            return False
        zone = ZoneInfo(self.time_zone)
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        begins = datetime.combine(date.fromisoformat(self.starts_on), datetime.min.time(), zone)
        finishes = datetime.combine(
            date.fromisoformat(self.ends_on) + timedelta(days=1), datetime.min.time(), zone
        )
        if not begins <= local_start < local_end <= finishes:
            return False
        # The one supported compound-location grammar is an exact spaced dash.
        # Whitespace/case normalization matches scan.same_place; no substring test.
        whole = " ".join(location.split()).casefold()
        components = {whole, *(part.strip() for part in whole.split(" - "))}
        return any(
            " ".join(label.split()).casefold() in components for label in self.venue_locations
        )


def _literal(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AttendanceError("Invalid attendance text; supply bounded nonempty literals")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise AttendanceError(
            "Invalid attendance text; remove control or invalid Unicode characters"
        )
    return value


def parse_attendance(value: object) -> Attendance:
    fields = {"id", "starts_on", "ends_on", "time_zone", "venue_locations"}
    if not isinstance(value, dict) or set(value) != fields:
        raise AttendanceError("Invalid attendance fields; use the documented schema-1 intake")
    identity = _literal(value["id"], 300)
    starts, ends = _literal(value["starts_on"], 10), _literal(value["ends_on"], 10)
    try:
        first, last = date.fromisoformat(starts), date.fromisoformat(ends)
        if (
            first.isoformat() != starts
            or last.isoformat() != ends
            or first > last
            or last == date.max
        ):
            raise ValueError("date span")
        zone = _literal(value["time_zone"], 100)
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise AttendanceError(
            "Invalid attendance dates/zone; supply ISO dates and an IANA zone"
        ) from exc
    locations = value["venue_locations"]
    if not isinstance(locations, list) or not 0 <= len(locations) <= 64:
        raise AttendanceError(
            "Invalid attendance labels; supply at most 64 exact venue/conference literals"
        )
    labels = tuple(_literal(item, 1000) for item in locations)
    if len({" ".join(item.split()).casefold() for item in labels}) != len(labels):
        raise AttendanceError("Duplicate attendance labels; remove duplicate normalized literals")
    return Attendance(identity, starts, ends, zone, labels)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AttendanceError("Duplicate JSON fields; repair the attendance input")
        result[key] = value
    return result


def decode_store(raw: bytes) -> tuple[Attendance, ...]:
    if len(raw) > MAX_BYTES:
        raise AttendanceError("Attendance input exceeds 256 KiB; reduce the recorded set")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise AttendanceError("Unreadable attendance JSON; repair the owner input") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "attendances"}:
        raise AttendanceError("Invalid attendance store; use its documented schema-1 owner")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise AttendanceError(
            "Unsupported attendance schema; upgrade the owner before planning/writing"
        )
    rows = value["attendances"]
    if not isinstance(rows, list) or len(rows) > 128:
        raise AttendanceError("Invalid attendance list; supply at most 128 records")
    parsed = tuple(parse_attendance(row) for row in rows)
    if len({row.id for row in parsed}) != len(parsed):
        raise AttendanceError("Duplicate attendance IDs; repair the owner input")
    return parsed


def load_attendances() -> tuple[Attendance, ...]:
    path = state_dir() / _FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AttendanceError("Attendance store is not a regular file; repair its owner path")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return decode_store(handle.read(MAX_BYTES + 1))
    finally:
        os.close(fd)


def record_attendance(value: object) -> Attendance:
    """Upsert one grounded row, preserving every other row under a private lock."""
    row = parse_attendance(value)
    directory = state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        directory / ".conference-attendance.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AttendanceError("Attendance lock is not a regular file; repair its owner path")
        fcntl.flock(fd, fcntl.LOCK_EX)
        prior = load_attendances()  # Unknown/corrupt data is never overwritten.
        rows = [asdict(item) for item in prior if item.id != row.id] + [asdict(row)]
        raw = json.dumps({"schema_version": 1, "attendances": rows}, ensure_ascii=False).encode(
            "utf-8"
        )
        decode_store(raw)
        tmp_fd, tmp = tempfile.mkstemp(dir=directory, prefix=".conference-attendance.")
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, directory / _FILENAME)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    finally:
        os.close(fd)
    return row


def attendance_for(rows: tuple[Attendance, ...], identity, location, start, end) -> str | None:
    matches = [row.id for row in rows if row.covers(identity, location, start, end)]
    if len(matches) > 1:
        raise AttendanceError("Ambiguous attendance match; consolidate overlapping owner records")
    return matches[0] if matches else None
