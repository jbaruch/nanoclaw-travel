"""Owner of explicit conference-presence planner inputs; no provider operations.

Only grounded intake records labels/dates. Calendar text is compared literally,
never classified as attendance by a conference name or city substring.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_VENUE_RADIUS_METERS = 500

MAX_BYTES = 256 * 1024
_FILENAME = "conference-attendance.json"


def state_dir() -> Path:
    """New drive-engine state; legacy drive-planner stores stay in their namespace."""
    return Path(os.environ.get("DRIVE_ENGINE_STATE_DIR", "/workspace/state/drive-engine"))


class AttendanceError(ValueError):
    """Repair the attendance input or upgrade its owner; never treat it as empty."""


@dataclass(frozen=True)
class AttendanceDay:
    date: str
    onsite_from: str
    onsite_to: str
    source: str
    source_ref: str | None

    def bounds(self, zone: str) -> tuple[datetime, datetime, datetime]:
        day = date.fromisoformat(self.date)
        tz = ZoneInfo(zone)
        return (
            _local_instant(day, self.onsite_from, tz),
            _local_instant(day, self.onsite_to, tz),
            datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC),
        )


@dataclass(frozen=True)
class Attendance:
    id: str
    starts_on: str
    ends_on: str
    time_zone: str
    venue_locations: tuple[str, ...]
    schema_version: int = 1
    venue_location: str | None = None
    days: tuple[AttendanceDay, ...] = ()

    def scopes_time(self, at: object) -> bool:
        """Only explicit row-2 dates alter base/return-anchor semantics."""
        return (
            self.schema_version == 2
            and isinstance(at, datetime)
            and at.tzinfo is not None
            and date.fromisoformat(self.starts_on)
            <= at.astimezone(ZoneInfo(self.time_zone)).date()
            <= date.fromisoformat(self.ends_on)
        )

    def covers_time(self, identity: object, start: object, end: object) -> bool:
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or len(identity) > 1024
            or _invalid_text(identity)
        ):
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
        if self.schema_version == 2 and not any(
            a <= start.astimezone(UTC) < end.astimezone(UTC) <= b
            for a, b, _ in (day.bounds(self.time_zone) for day in self.days)
        ):
            return False
        return True

    def covers(
        self,
        identity: object,
        location: object,
        start: object,
        end: object,
        *,
        near: frozenset[tuple[str, str]] = frozenset(),
    ) -> bool:
        if not isinstance(location, str) or not self.covers_time(identity, start, end):
            return False
        # `near` holds (venue, location) pairs within the venue radius by
        # driving distance (#373): another room of the same venue.
        if (
            self.schema_version == 2
            and self.venue_locations
            and (self.venue_location, location) in near
        ):
            return True
        # The one supported compound-location grammar is an exact spaced dash.
        # Whitespace/case normalization matches scan.same_place; no substring test.
        whole = " ".join(location.split()).casefold()
        components = {whole, *(part.strip() for part in whole.split(" - "))}
        return any(
            " ".join(label.split()).casefold() in components for label in self.venue_locations
        )


def _invalid_text(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Cn"} for char in value)


def _literal(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AttendanceError("Invalid attendance text; supply bounded nonempty literals")
    if _invalid_text(value):
        raise AttendanceError(
            "Invalid attendance text; remove control or invalid Unicode characters"
        )
    return value


def _local_instant(day: date, value: str, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(day, time.fromisoformat(value))
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if (
        first.utcoffset() != second.utcoffset()
        or first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive
    ):
        raise AttendanceError("Ambiguous or nonexistent attendance boundary; supply other hours")
    return first.astimezone(UTC)


def _parse_day(value: object, starts: date, ends: date, zone: str) -> AttendanceDay:
    if not isinstance(value, dict) or set(value) != {
        "date",
        "onsite_from",
        "onsite_to",
        "source",
        "source_ref",
    }:
        raise AttendanceError("Invalid attendance day; use the documented schema-2 window")
    raw_date = _literal(value["date"], 10)
    try:
        day = date.fromisoformat(raw_date)
        if day.isoformat() != raw_date or not starts <= day <= ends:
            raise ValueError("date")
        for key in ("onsite_from", "onsite_to"):
            raw = _literal(value[key], 5)
            parsed = time.fromisoformat(raw)
            if len(raw) != 5 or parsed.isoformat(timespec="minutes") != raw:
                raise ValueError("time")
        if value["onsite_from"] >= value["onsite_to"]:
            raise ValueError("window")
    except ValueError as exc:
        raise AttendanceError(
            "Invalid attendance day/time; supply an in-span date and HH:MM window"
        ) from exc
    source, reference = value["source"], value["source_ref"]
    if source == "agenda":
        reference = _literal(reference, 2000)
        try:
            parsed_ref = urlsplit(reference)
            if (
                reference != reference.strip()
                or parsed_ref.scheme != "https"
                or not parsed_ref.hostname
                or parsed_ref.username is not None
                or parsed_ref.password is not None
            ):
                raise ValueError("reference")
        except ValueError as exc:
            raise AttendanceError(
                "Agenda hours require a cited HTTPS conference schedule reference"
            ) from exc
    elif source != "owner" or reference is not None:
        raise AttendanceError("Attendance hours need owner confirmation or a cited actual agenda")
    result = AttendanceDay(raw_date, value["onsite_from"], value["onsite_to"], source, reference)
    result.bounds(zone)
    return result


def attendance_record(row: Attendance) -> dict:
    """Explicit row discriminator keeps legacy presence distinct from empty windows."""
    value = asdict(row)
    value["venue_locations"] = list(row.venue_locations)
    value["days"] = [asdict(day) for day in row.days]
    if row.schema_version == 1:
        del value["venue_location"]
        del value["days"]
    return value


def parse_attendance(value: object) -> Attendance:
    fields = {"id", "starts_on", "ends_on", "time_zone", "venue_locations"}
    if not isinstance(value, dict):
        raise AttendanceError("Invalid attendance fields; use the documented owner intake")
    version = value.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise AttendanceError("Unsupported attendance row; update its owner before writing")
    expected = fields | ({"schema_version", "venue_location", "days"} if version == 2 else set())
    if set(value) != expected and not (version == 1 and set(value) == fields | {"schema_version"}):
        raise AttendanceError("Invalid attendance fields; use the documented owner intake")
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
    if version == 1:
        return Attendance(identity, starts, ends, zone, labels)
    venue = _literal(value["venue_location"], 1000)
    _local_instant(first, "00:00", ZoneInfo(zone))
    _local_instant(last + timedelta(days=1), "00:00", ZoneInfo(zone))
    raw_days = value["days"]
    if not isinstance(raw_days, list) or len(raw_days) > 128:
        raise AttendanceError("Invalid attendance days; use a bounded explicit day list")
    days = tuple(_parse_day(item, first, last, zone) for item in raw_days)
    if len({day.date for day in days}) != len(days):
        raise AttendanceError("Duplicate attendance days; supply one window per local day")
    return Attendance(identity, starts, ends, zone, labels, 2, venue, days)


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
        raise AttendanceError("Invalid attendance store; use its documented owner")
    if type(value["schema_version"]) is not int or value["schema_version"] not in (1, 2):
        raise AttendanceError(
            "Unsupported attendance schema; upgrade the owner before planning/writing"
        )
    rows = value["attendances"]
    if not isinstance(rows, list) or len(rows) > 128:
        raise AttendanceError("Invalid attendance list; supply at most 128 records")
    if value["schema_version"] == 2 and any(
        not isinstance(row, dict) or "schema_version" not in row for row in rows
    ):
        raise AttendanceError("Schema-2 store requires an explicit version on every retained row")
    if value["schema_version"] == 1 and any(
        not isinstance(row, dict) or "schema_version" in row for row in rows
    ):
        raise AttendanceError("Schema-1 store accepts only original legacy rows")
    parsed = tuple(parse_attendance(row) for row in rows)
    if len({row.id for row in parsed}) != len(parsed):
        raise AttendanceError("Duplicate attendance IDs; repair the owner input")
    return parsed


def _identity() -> tuple[int, int]:
    """Host callers resolve runtime config first; this is its effective agent user."""
    identity = os.geteuid(), os.getegid()
    if identity[0] == 0:
        raise AttendanceError(
            "Refusing root writer; invoke as the runtime's normalized agent UID/GID"
        )
    return identity


def _verify(info, identity, mode: int, *, directory: bool = False, device=None) -> None:
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not kind_ok
        or (not directory and info.st_nlink != 1)
        or (info.st_uid, info.st_gid) != identity
        or stat.S_IMODE(info.st_mode) != mode
        or (device is not None and info.st_dev != device)
    ):
        raise AttendanceError(
            "Attendance ownership/mode/type mismatch; repair only the verified agent path"
        )


def _read_at(directory_fd: int, identity=None) -> tuple[Attendance, ...]:
    try:
        fd = os.open(_FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return ()
    try:
        info = os.fstat(fd)
        if identity is not None:
            _verify(info, identity, 0o600, device=os.fstat(directory_fd).st_dev)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AttendanceError(
                "Attendance store is not a single-link regular file; repair its owner path"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return decode_store(handle.read(MAX_BYTES + 1))
    finally:
        os.close(fd)


def load_attendances() -> tuple[Attendance, ...]:
    try:
        directory_fd = os.open(state_dir(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ()
    try:
        return _read_at(directory_fd)
    finally:
        os.close(directory_fd)


def record_attendance(value: object) -> Attendance:
    """Upsert one grounded row under a verified directory/lock; never run as root."""
    row = parse_attendance(value)
    identity = _identity()
    directory = state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_info = os.fstat(directory_fd)
        _verify(directory_info, identity, 0o700, directory=True)
        lock_name = ".conference-attendance.lock"
        lock_fd = os.open(
            lock_name,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            _verify(os.fstat(lock_fd), identity, 0o600, device=directory_info.st_dev)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            prior = _read_at(directory_fd, identity)  # Never overwrite unknown/corrupt data.
            rows = [attendance_record(item) for item in prior if item.id != row.id] + [
                attendance_record(row)
            ]
            raw = json.dumps({"schema_version": 2, "attendances": rows}, ensure_ascii=False).encode(
                "utf-8"
            )
            decode_store(raw)
            # Exclusive creation and rename both use the held directory, not a
            # pathname re-resolution between validation and the private write.
            tmp = ".conference-attendance." + secrets.token_hex(16)
            tmp_fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            temporary_exists = True
            written = None
            try:
                written = os.fstat(tmp_fd)
                _verify(written, identity, 0o600, device=directory_info.st_dev)
                with os.fdopen(tmp_fd, "wb", closefd=False) as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, _FILENAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                temporary_exists = False
                os.fsync(directory_fd)
                final_fd = os.open(
                    _FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
                )
                try:
                    final = os.fstat(final_fd)
                    _verify(final, identity, 0o600, device=directory_info.st_dev)
                    if (final.st_dev, final.st_ino) != (written.st_dev, written.st_ino):
                        raise AttendanceError(
                            "Attendance replacement changed; inspect concurrent writers"
                        )
                    with os.fdopen(final_fd, "rb", closefd=False) as handle:
                        if handle.read(MAX_BYTES + 1) != raw:
                            raise AttendanceError(
                                "Attendance readback differs; inspect concurrent writers"
                            )
                    _verify(os.fstat(final_fd), identity, 0o600, device=directory_info.st_dev)
                finally:
                    os.close(final_fd)
                actual_dir = directory.stat(follow_symlinks=False)
                lock = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
                if (actual_dir.st_dev, actual_dir.st_ino) != (
                    directory_info.st_dev,
                    directory_info.st_ino,
                ) or (lock.st_dev, lock.st_ino) != (
                    os.fstat(lock_fd).st_dev,
                    os.fstat(lock_fd).st_ino,
                ):
                    raise AttendanceError(
                        "Attendance directory/lock changed; inspect concurrent writers"
                    )
                _verify(os.fstat(lock_fd), identity, 0o600, device=directory_info.st_dev)
                _verify(
                    os.fstat(directory_fd),
                    identity,
                    0o700,
                    directory=True,
                    device=directory_info.st_dev,
                )
            finally:
                os.close(tmp_fd)
                if temporary_exists and written is not None:
                    temporary = os.stat(tmp, dir_fd=directory_fd, follow_symlinks=False)
                    if (temporary.st_dev, temporary.st_ino) != (written.st_dev, written.st_ino):
                        raise AttendanceError(
                            "Attendance temporary changed; inspect concurrent writers"
                        )
                    os.unlink(tmp, dir_fd=directory_fd)
        finally:
            os.close(lock_fd)
    finally:
        os.close(directory_fd)
    return row


def attendance_for(
    rows: tuple[Attendance, ...],
    identity,
    location,
    start,
    end,
    *,
    near: frozenset[tuple[str, str]] = frozenset(),
) -> str | None:
    matches = [row.id for row in rows if row.covers(identity, location, start, end, near=near)]
    if len(matches) > 1:
        raise AttendanceError("Ambiguous attendance match; consolidate overlapping owner records")
    return matches[0] if matches else None
