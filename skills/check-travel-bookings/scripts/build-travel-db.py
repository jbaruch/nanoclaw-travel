#!/usr/bin/env python3
"""
Build a per-trip, per-day travel database from travel-schedule.json.

Reads the flat event list produced by refresh-travel-schedule.py and
organises it into trips with all their items indexed by start date.
ALL item types are stored; alert logic lives in the consumers.

Output: /workspace/group/travel-db.json
Schema (see sibling `state-schema.md` for the full contract):
  {
    "schema_version": 3,
    "generated_at": "...",
    "trips": {
      "<slug>": {
        "summary": "...",
        "start":   "YYYY-MM-DD",                       # trip-level: date-only
        "end":     "YYYY-MM-DD",
        "destination": "Nashville, TN",                # v3, optional
        "days": {
          "YYYY-MM-DD": [               # day key: date-only, local when known
            {"type": "Flight|Lodging|Rail|Car Rental|...",
             "summary": "...",
             "start": "YYYY-MM-DD" | "YYYY-MM-DDTHH:MM:SSZ",  # item-level: timed VEVENTs carry time
             "end":   "YYYY-MM-DD" | "YYYY-MM-DDTHH:MM:SSZ",
             "start_local": "YYYY-MM-DDTHH:MM:SS±HH:MM",      # v2, optional
             "end_local":   "YYYY-MM-DDTHH:MM:SS±HH:MM",      # v2, optional
             "uid":   "..."}
          ]
        }
      }
    }
  }

`start`/`end` stay the UTC instants the feed stamps. `start_local`/`end_local`
are the same instants on the traveller's own clock, present only when the
schedule resolved them (see `nightly-travel-sync/scripts/tripit_local_time.py`).
Date-granular consumers read the local field and fall back to the UTC one.

An item lands in a trip by its local-first days (the same rule as the day key).
A transport item (`_TRANSPORT_TYPES`) is a point event and files under a trip
only when its departure day is inside `[start, end]`; every other item type
files under each trip its `[start day, end day]` span overlaps (#293). A
transport segment on a day two adjacent trips share still files under both.

`destination` is the trip wrapper's TripIt primary location (`<City>, <Region>`)
as the schedule carries it — the writer decodes the feed's ICS escapes (#275) —
omitted when the feed leaves it blank. It is what separates a local placeholder
trip — one the operator files to block time for a Nashville event, with nothing
to book — from an away trip that genuinely has no bookings yet (#271).
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# travel-core hosts the canonical trip key so the DB's slugs and the drive
# engine's per-trip verdicts agree. Runtime mount first, dev-clone sibling
# fallback for CI (the cross-bundle pattern in travel-core's SKILL.md; this
# script sits one level deeper, under `scripts/`).
_BUNDLE_DIR = Path(__file__).resolve().parent.parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from trip_key import trip_key  # noqa: E402

SCHEDULE_PATH = "/workspace/group/travel-schedule.json"
DB_PATH = "/workspace/group/travel-db.json"

# Bump in lock-step with check-travel-bookings.py per
# `coding-policy: stateful-artifacts` + state-schema.md sibling file.
SCHEMA_VERSION = 3

# Point-event item types: a segment belongs to the day it departs, so it files
# under a trip by that day alone, never by an end instant that crosses midnight
# into the next trip's window (#293). Mirrors the transport set
# check-travel-bookings.py counts as "has_transport".
_TRANSPORT_TYPES = frozenset({"Flight", "Rail"})


def _local_first(item: dict, field: str) -> str:
    """The `<field>_local` stamp when the schedule resolved one, else `<field>`.

    The day key already follows the traveller's local clock (#268); the
    trip-assignment test reads the same value, so a red-eye that lands after
    midnight UTC does not overlap a trip it never touched on the ground (#293).
    """
    local = item.get(f"{field}_local")
    if isinstance(local, str) and local:
        return local
    return item[field]


def _belongs_to_trip(item: dict, trip_start: date, trip_end: date) -> bool:
    """Whether `item` files under the trip spanning `[trip_start, trip_end]`.

    Transport is a point event: in iff its local-first departure day is inside
    the window. Everything else (lodging, rentals, novel types) is a span: in
    iff `[start day, end day]` overlaps the window.
    """
    item_start = _parse_day(_local_first(item, "start"))
    if item["type"] in _TRANSPORT_TYPES:
        return trip_start <= item_start <= trip_end
    item_end = _parse_day(_local_first(item, "end"))
    return item_start <= trip_end and item_end >= trip_start


def _parse_day(s: str) -> date:
    # Tolerate both shapes emitted by refresh-travel-schedule.py:
    # date-only `YYYY-MM-DD` (trip-level wrappers, VEVENTs with
    # `VALUE=DATE`) and ISO datetime `YYYY-MM-DDTHH:MM:SSZ` (timed
    # VEVENTs — flights, lodging check-ins, rentals — preserved by
    # `nanoclaw-admin#289`). Day-keyed grouping is by calendar date,
    # so the time component is intentionally discarded here. The
    # untruncated value lives on in each item's `start`/`end` field
    # for consumers that need the actual departure time.
    return date.fromisoformat(s[:10])


def main():
    try:
        with open(SCHEDULE_PATH, encoding="utf-8") as f:
            events = json.load(f)
    except FileNotFoundError:
        print(
            f"ERROR: {SCHEDULE_PATH} not found — run refresh-travel-schedule.py first",
            file=sys.stderr,
        )
        sys.exit(1)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Corrupt, non-UTF-8, or partially-written schedule (the writer
        # crashed mid-flight), or an access-level failure (permissions,
        # I/O error). The named exception tells the operator which; the
        # rewrite path fixes content problems, not access problems.
        print(
            f"ERROR: cannot read {SCHEDULE_PATH} as JSON "
            f"({type(exc).__name__}: {exc}) — for a corrupt or partial file, "
            "re-run refresh-travel-schedule.py to rewrite it; for an access "
            "error, fix the file's permissions/mount first",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        print(
            f"ERROR: {SCHEDULE_PATH} root must be a JSON array of event objects "
            "(the refresh-travel-schedule.py output contract) — re-run "
            "refresh-travel-schedule.py to rewrite it",
            file=sys.stderr,
        )
        sys.exit(1)

    today = date.today()

    trips_raw = [e for e in events if "item-" not in e.get("uid", "")]
    items_raw = [e for e in events if "item-" in e.get("uid", "")]

    db_trips = {}
    for trip in trips_raw:
        trip_end = _parse_day(trip["end"])
        if trip_end < today:
            continue

        trip_start = _parse_day(trip["start"])
        slug = trip_key(trip["summary"], trip["start"])

        # Items that belong to this trip — see `_belongs_to_trip` (#293).
        days: dict[str, list] = {}
        for item in items_raw:
            if _belongs_to_trip(item, trip_start, trip_end):
                entry = {
                    "type": item["type"],
                    "summary": item["summary"],
                    "start": item["start"],
                    "end": item["end"],
                    "uid": item["uid"],
                }
                # v2: carry the schedule's v3 local clock through untouched.
                # The day key follows it when present, so a red-eye out of San
                # Francisco at 11:05 PM files under the night it leaves rather
                # than the UTC morning it becomes (#268).
                for field in ("start_local", "end_local"):
                    value = item.get(field)
                    if isinstance(value, str) and value:
                        entry[field] = value
                day_key = (entry.get("start_local") or entry["start"])[:10]
                days.setdefault(day_key, []).append(entry)

        # Sort each day's events by type for readability
        TYPE_ORDER = {"Flight": 0, "Rail": 1, "Lodging": 2, "Car Rental": 3}
        for day_events in days.values():
            day_events.sort(key=lambda e: (TYPE_ORDER.get(e["type"], 9), e["summary"]))

        db_trips[slug] = {
            "summary": trip["summary"],
            "start": trip["start"],
            "end": trip["end"],
            "days": dict(sorted(days.items())),  # sorted by date
        }

        # v3: the trip's own destination, so a consumer can tell a local
        # placeholder from an away trip (#271). Written only when the feed
        # labels the trip — an absent key reads as "destination unknown", which
        # no consumer may treat as home. Copied through as the schedule carries
        # it: decoding the feed's ICS escapes is the writer's job now (#275),
        # and a second pass here would eat a literal backslash in an address.
        location = trip.get("location")
        if isinstance(location, str) and location.strip():
            db_trips[slug]["destination"] = location.strip()

    # Forward-incompatibility guard per state-schema.md migration
    # policy: if a future writer has already stamped travel-db.json
    # with a higher schema_version, do NOT overwrite it with this
    # older writer's output. Best-effort read — any error (file
    # missing, malformed, no schema_version) means "no forward state,
    # safe to write".
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            existing = json.load(f)
        existing_version = existing.get("schema_version") if isinstance(existing, dict) else None
        if (
            isinstance(existing_version, int)
            and not isinstance(existing_version, bool)
            and existing_version > SCHEMA_VERSION
        ):
            print(
                f"ERROR: existing {DB_PATH} has schema_version={existing_version} > "
                f"writer's {SCHEMA_VERSION}; refusing to downgrade. Upgrade "
                "this skill (`tessl__check-travel-bookings`) before re-running "
                "`nightly-travel-sync` Step 4.",
                file=sys.stderr,
            )
            sys.exit(2)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass  # No forward state on disk; proceed with write.

    db = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trips": db_trips,
    }

    # Atomic write: same-dir `.tmp` sibling + `os.replace`. Matches the
    # `_atomic_write_json` pattern in `skills/flight-assist/state.py`.
    # Uses normal `open(...)` so file mode follows the process umask
    # (the cross-plugin readers — `morning-brief`, `check-travel-bookings`
    # — share the group volume but may run under different UIDs at
    # times; `tempfile.mkstemp`'s 0o600 default would break those reads).
    tmp_path = DB_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, DB_PATH)

    total_items = sum(len(evts) for t in db_trips.values() for evts in t["days"].values())
    trip_summary = []
    for slug, t in sorted(db_trips.items(), key=lambda x: x[1]["start"]):
        type_counts: dict[str, int] = {}
        for evts in t["days"].values():
            for ev in evts:
                type_counts[ev["type"]] = type_counts.get(ev["type"], 0) + 1
        trip_summary.append(
            {
                "slug": slug,
                "summary": t["summary"],
                "start": t["start"],
                "end": t["end"],
                # Carried into the run summary so an operator reading the
                # nightly log can see which trips the booking check will read
                # as local, without opening the DB.
                "destination": t.get("destination", ""),
                "type_counts": type_counts,
            }
        )

    # Structured JSON output per `coding-policy: script-delegation`
    # (Script Requirements: JSON-producing). Operators reading the
    # logs see the same shape regardless of trip count; downstream
    # consumers (host-side audits, future cross-plugin checks) can
    # parse without ad-hoc prose-line regexes.
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "trips_written": len(db_trips),
                "item_events_written": total_items,
                "trips": trip_summary,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
