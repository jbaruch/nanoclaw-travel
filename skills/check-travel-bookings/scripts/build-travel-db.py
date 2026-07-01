#!/usr/bin/env python3
"""
Build a per-trip, per-day travel database from travel-schedule.json.

Reads the flat event list produced by refresh-travel-schedule.py and
organises it into trips with all their items indexed by start date.
ALL item types are stored; alert logic lives in the consumers.

Output: /workspace/group/travel-db.json
Schema (see sibling `state-schema.md` for the full contract):
  {
    "schema_version": 1,
    "generated_at": "...",
    "trips": {
      "<slug>": {
        "summary": "...",
        "start":   "YYYY-MM-DD",                       # trip-level: date-only
        "end":     "YYYY-MM-DD",
        "days": {
          "YYYY-MM-DD": [                              # day key: always date-only
            {"type": "Flight|Lodging|Rail|Car Rental|...",
             "summary": "...",
             "start": "YYYY-MM-DD" | "YYYY-MM-DDTHH:MM:SSZ",  # item-level: timed VEVENTs carry time
             "end":   "YYYY-MM-DD" | "YYYY-MM-DDTHH:MM:SSZ",
             "uid":   "..."}
          ]
        }
      }
    }
  }
"""

import json
import os
import re
import sys
from datetime import date, datetime, timezone

SCHEDULE_PATH = "/workspace/group/travel-schedule.json"
DB_PATH = "/workspace/group/travel-db.json"

# Bump in lock-step with check-travel-bookings.py per
# `coding-policy: stateful-artifacts` + state-schema.md sibling file.
SCHEMA_VERSION = 1


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


def make_slug(summary: str, start_str: str) -> str:
    start = _parse_day(start_str)
    clean = re.sub(r"\s+\d{4}$", "", summary.strip())
    slug_base = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-")
    return f"{slug_base}-{start.year}-{start.month:02d}"


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

    today = date.today()

    trips_raw = [e for e in events if "item-" not in e.get("uid", "")]
    items_raw = [e for e in events if "item-" in e.get("uid", "")]

    db_trips = {}
    for trip in trips_raw:
        trip_end = _parse_day(trip["end"])
        if trip_end < today:
            continue

        trip_start = _parse_day(trip["start"])
        slug = make_slug(trip["summary"], trip["start"])

        # Items that overlap with this trip's date range
        days: dict[str, list] = {}
        for item in items_raw:
            item_start = _parse_day(item["start"])
            item_end = _parse_day(item["end"])
            # Overlap check: item starts before trip ends AND item ends on/after trip starts
            if item_start <= trip_end and item_end >= trip_start:
                day_key = item["start"][:10]
                days.setdefault(day_key, []).append(
                    {
                        "type": item["type"],
                        "summary": item["summary"],
                        "start": item["start"],
                        "end": item["end"],
                        "uid": item["uid"],
                    }
                )

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
