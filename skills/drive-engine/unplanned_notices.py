"""Tell the operator, once, about a drive the engine keeps failing to plan (#334, #335).

A held leg (`reconcile.HeldLeg`) that still has a block on the calendar keeps
that block, so the operator sees a drive. One with no block is a drive that is
simply missing, and nothing said so: the Nov 2 home→BNA drive was absent from
the calendar with only a stderr line behind it (#335).

`due_notices` decides which missing legs to mention. A leg is mentioned once it
has been missing for `PERSIST_BEFORE_NOTICE` (a one-sweep routing blip heals on
the next sweep and is not worth a message) and only once per spell. An overlap
notice (#391) is no routing blip: its drives are already deleted, so it is
mentioned on the sweep that first sees it. An entry that plans again is
forgotten, so a later failure is news again. Only legs due
within `NOTICE_HORIZON` count; a drive months out has time to resolve itself.
An overlap notice's `when` is its first meeting's end, so it counts however
far out that is: its drives are already gone.

Each schema version keeps its own file (`unplanned-notices.v<N>.json`), so
engines of different versions never read or write each other's record: after
an upgrade or a rollback the running engine starts or resumes its own, and
the cost is at most one repeated notice. Hint, not authority
(`coding-policy: stateful-artifacts`): a missing, unreadable or malformed file
reads as "nothing noticed yet", with a stderr diagnostic for anything but a
plain missing file. The cost is one repeated notice, never a missed drive.
Schema in `state-schema.md`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

# E402: these bundle-local imports resolve only after the sys.path insert above.
from meeting_source import OVERLAP_NOTICE_KIND  # noqa: E402
from reconcile import HeldLeg  # noqa: E402
from skip_state import state_dir  # noqa: E402

NOTICES_SCHEMA_VERSION = 1
_NOTICES_FILE = f"unplanned-notices.v{NOTICES_SCHEMA_VERSION}.json"

# Two sweeps run 30 minutes apart; a leg still missing on the third has
# outlasted a routing blip.
PERSIST_BEFORE_NOTICE = timedelta(minutes=55)
NOTICE_HORIZON = timedelta(days=14)
# An entry outlives its leg's time by this much, then is dropped.
_EXPIRY_GRACE = timedelta(days=1)


def _path() -> Path:
    return state_dir() / _NOTICES_FILE


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


def _key(leg: HeldLeg) -> str:
    return f"{leg.kind}|{leg.identity}"


def _load() -> dict[str, dict]:
    path = _path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(
            f"[drive-engine] unplanned-notices {path} unreadable ({exc}); starting fresh",
            file=sys.stderr,
        )
        return {}
    version = payload.get("schema_version") if isinstance(payload, dict) else None
    if type(version) is not int or version != NOTICES_SCHEMA_VERSION:
        print(
            f"[drive-engine] unplanned-notices {path} is not schema_version "
            f"{NOTICES_SCHEMA_VERSION}; starting fresh",
            file=sys.stderr,
        )
        return {}
    legs = payload.get("legs")
    if not isinstance(legs, dict):
        print(
            f"[drive-engine] unplanned-notices {path} has no `legs` object; starting fresh "
            "(a missing drive may be announced once more)",
            file=sys.stderr,
        )
        return {}
    kept: dict[str, dict] = {}
    for key, entry in legs.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        first_seen, expires = _parse(entry.get("first_seen")), _parse(entry.get("expires"))
        if first_seen is None or expires is None or not isinstance(entry.get("notified"), bool):
            continue
        kept[key] = {"first_seen": first_seen, "expires": expires, "notified": entry["notified"]}
    return kept


def _store(entries: dict[str, dict]) -> None:
    payload = {
        "schema_version": NOTICES_SCHEMA_VERSION,
        "legs": {
            key: {
                "first_seen": _stamp(entry["first_seen"]),
                "expires": _stamp(entry["expires"]),
                "notified": entry["notified"],
            }
            for key, entry in sorted(entries.items())
        },
    }
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def due_notices(missing: list[HeldLeg], *, now: datetime) -> list[HeldLeg]:
    """The missing legs to mention this sweep, recording that they were.

    `missing` are this sweep's held legs that have no block on the calendar.
    Raises OSError when the state file cannot be written; the caller decides
    whether a failed write may cost a repeated notice.
    """
    if now.tzinfo is None:
        raise ValueError("due_notices: `now` must be timezone-aware")
    entries = _load()
    current: dict[str, HeldLeg] = {}
    for leg in missing:
        if leg.when is None or leg.when < now:
            continue
        if leg.when > now + NOTICE_HORIZON and leg.kind != OVERLAP_NOTICE_KIND:
            continue
        current.setdefault(_key(leg), leg)

    updated: dict[str, dict] = {}
    due: list[HeldLeg] = []
    for key, leg in current.items():
        assert leg.when is not None
        entry = entries.get(key)
        if entry is None or entry["expires"] <= now:
            entry = {"first_seen": now, "expires": leg.when + _EXPIRY_GRACE, "notified": False}
        persisted = now - entry["first_seen"] >= PERSIST_BEFORE_NOTICE
        if not entry["notified"] and (persisted or leg.kind == OVERLAP_NOTICE_KIND):
            entry = {**entry, "notified": True}
            due.append(leg)
        updated[key] = entry

    if updated != entries:
        _store(updated)
    return sorted(due, key=lambda leg: (leg.when or now, leg.identity))
