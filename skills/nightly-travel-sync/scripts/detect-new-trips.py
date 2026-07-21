#!/usr/bin/env python3
"""
Detect trips that appeared in travel-db.json since the last nightly run
and emit one durable daily-log line per new trip (log-only, no chat).

`nightly-travel-sync` already surfaces timezone changes, OOO, trip
conflicts, and booking gaps to chat. A newly-appeared trip that is
already fully booked, with no TZ change and no conflict, enters
silently — nothing durable records it, so the main agent has no
awareness of it until it queries the travel files on demand (#204).

This script closes that gap deterministically: it diffs the current
travel-db.json trip set against a persisted snapshot
(travel-trips-seen.json) and reports the trips that are new. The
skill logs each one to the group daily memory and then calls this
script again with `--commit` to persist the snapshot. There is no
chat output by design — the owner books the trips himself and a
per-trip ping is noise; the requirement is durable *agent* awareness.

Two modes:

  detect (default)
    Read travel-db.json + travel-trips-seen.json, compute the new
    trips, print a JSON object, and touch nothing on disk:
      {"seeded": bool, "new_trips": [
        {"slug", "summary", "start", "end", "log_line"}, ...]}
    - `seeded` is true when no usable prior snapshot exists (absent,
      corrupt, or an unrecognised schema_version). On a seed run
      `new_trips` is always empty: the whole itinerary is NOT dumped
      as "new" (#204). The caller logs nothing and just commits the
      snapshot, so only trips appearing *after* the seed are new.
    - `log_line` is the semantic content the daily log records,
      WITHOUT the leading `- HH:MM UTC` timestamp — the caller
      prepends the timestamp (the trusted-memory daily-log
      convention). It carries no clock value, so this stays testable.

  --commit
    Rewrite travel-trips-seen.json to exactly the current upcoming
    trip set and print {"committed": true, "trips_tracked": N}. The
    skill runs this AFTER logging, so a logging failure leaves the
    snapshot untouched and the next nightly run retries; the daily-log
    helper's own line-dedup guards against a double entry in the
    meantime. The snapshot is pruned to the current set each commit,
    so a trip that ages out (past/cancelled) simply drops — it is not
    a "new trip", and cancellation logging is out of scope here.

Both modes scope to upcoming trips (`end` on/after today), mirroring
build-travel-db.py, so a trip dropping into the past never counts as
appearing or disappearing.

Reads:  /workspace/group/travel-db.json
        /workspace/group/travel-trips-seen.json
Writes: /workspace/group/travel-trips-seen.json  (--commit only)
"""

import json
import os
import sys
from datetime import date, datetime, timezone

DB_PATH = "/workspace/group/travel-db.json"
SEEN_PATH = "/workspace/group/travel-trips-seen.json"

# Snapshot schema — see the sibling `state-schema.md`
# (travel-trips-seen.json section). Bump in lock-step with that doc.
SCHEMA_VERSION = 1


def _parse_day(s: str) -> date:
    # travel-db.json trip-level start/end are date-only `YYYY-MM-DD`
    # (build-travel-db.py). Slice defensively in case a timed value
    # ever reaches here.
    return date.fromisoformat(s[:10])


def _load_db_trips() -> dict:
    """Return {slug: {"summary", "start", "end"}} for upcoming trips
    in travel-db.json (`end` on/after today). Exit 1 with an
    actionable diagnostic if the DB is missing or unreadable — Step 4
    (build-travel-db.py) rebuilds it immediately before this runs, so
    an absent/corrupt DB here is a real failure, not a first-run
    state."""
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            db = json.load(f)
    except FileNotFoundError:
        print(
            f"ERROR: {DB_PATH} not found — run "
            "check-travel-bookings/scripts/build-travel-db.py "
            "(nightly-travel-sync Step 4) first",
            file=sys.stderr,
        )
        sys.exit(1)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(
            f"ERROR: cannot read {DB_PATH} as JSON "
            f"({type(exc).__name__}: {exc}) — re-run build-travel-db.py "
            "to rewrite it",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(db, dict) or not isinstance(db.get("trips"), dict):
        print(
            f"ERROR: {DB_PATH} must be a JSON object with a `trips` map "
            "(the build-travel-db.py output contract) — re-run "
            "build-travel-db.py to rewrite it",
            file=sys.stderr,
        )
        sys.exit(1)

    today = date.today()
    upcoming = {}
    for slug, trip in db["trips"].items():
        try:
            trip_end = _parse_day(trip["end"])
        except (KeyError, TypeError, ValueError):
            # A malformed trip record in an otherwise-valid DB: skip it
            # rather than abort the whole run. build-travel-db.py owns
            # the shape; a bad record there is its bug to surface.
            continue
        if trip_end < today:
            continue
        upcoming[slug] = {
            "summary": trip.get("summary", slug),
            "start": trip.get("start", ""),
            "end": trip.get("end", ""),
        }
    return upcoming


def _load_prior_slugs() -> set | None:
    """Return the set of previously-seen trip slugs, or None when no
    usable prior snapshot exists (absent, corrupt, non-object, or an
    unrecognised schema_version). None triggers a seed run — the whole
    itinerary is never dumped as new (#204), and --commit rewrites a
    clean snapshot at the current version."""
    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(snap, dict):
        return None
    if snap.get("schema_version") != SCHEMA_VERSION:
        # Owner-side safe fallback per `coding-policy: stateful-artifacts`:
        # an unfamiliar version is treated as "no usable prior state".
        # --commit rewrites at SCHEMA_VERSION. Only v1 exists today, so
        # this fires only on a corrupt/hand-edited version field.
        return None
    trips = snap.get("trips")
    if not isinstance(trips, dict):
        return None
    return set(trips.keys())


def _make_log_line(trip: dict) -> str:
    """Semantic daily-log content for a new trip, WITHOUT the leading
    `- HH:MM UTC` timestamp (the caller prepends it). Uses only fields
    always present on a travel-db.json trip record — summary carries
    the destination; route/city enrichment is intentionally omitted
    (travel-db.json trip records hold no reliable origin→dest)."""
    start, end = trip["start"], trip["end"]
    if start and end:
        span = f" ({start} to {end})"
    elif start or end:
        span = f" ({start or end})"
    else:
        span = ""
    return f"[travel] new trip: {trip['summary']}{span}"


def _write_snapshot(trips: dict) -> None:
    """Atomically rewrite travel-trips-seen.json to the given trip set.
    Same-dir `.tmp` + os.replace, normal open() so mode follows the
    umask (the group volume is shared across plugin readers), matching
    build-travel-db.py's write."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trips": trips,
    }
    tmp_path = SEEN_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, SEEN_PATH)


def _commit() -> None:
    trips = _load_db_trips()
    _write_snapshot(trips)
    print(json.dumps({"committed": True, "trips_tracked": len(trips)}, ensure_ascii=False))


def _detect() -> None:
    current = _load_db_trips()
    prior = _load_prior_slugs()

    if prior is None:
        # Seed run: record nothing, report seeded so the caller skips
        # logging and just commits the snapshot.
        print(json.dumps({"seeded": True, "new_trips": []}, ensure_ascii=False))
        return

    new_slugs = [slug for slug in current if slug not in prior]
    new_trips = []
    for slug in sorted(new_slugs, key=lambda s: (current[s]["start"], s)):
        trip = current[slug]
        new_trips.append(
            {
                "slug": slug,
                "summary": trip["summary"],
                "start": trip["start"],
                "end": trip["end"],
                "log_line": _make_log_line(trip),
            }
        )

    print(json.dumps({"seeded": False, "new_trips": new_trips}, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--commit"]:
        _commit()
    elif not args:
        _detect()
    else:
        print(
            f"ERROR: unknown arguments {args!r} — usage: detect-new-trips.py [--commit]",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
