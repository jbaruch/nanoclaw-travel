#!/usr/bin/env python3
"""
Travel booking gap checker — reads from travel-db.json.

travel-db.json is built nightly by build-travel-db.py inside
`nightly-travel-sync` Step 4 ("Rebuild travel-db.json from the
schedule") in the `jbaruch/nanoclaw-travel` plugin. A missing, unreadable,
or structurally invalid DB is a hard error: that Step 4's failure
branch — an `mcp__nanoclaw__send_message` notification, with the next
daily cron re-running the bundle — is the correct alerting surface for
DB issues. A silent live-ICS fallback here would only mask that signal.
(The two-tier freshness probe in `nightly-travel-sync` Step 3 is for
`travel-schedule.json`, not the DB.)

Alerts on transport (Flight or Rail) + Lodging gaps; all item types are in the DB for future use.
"""

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

DB_PATH = "/workspace/group/travel-db.json"
STATE_PATH = "/workspace/group/travel-booking-state.json"

# Bump in lock-step with build-travel-db.py per
# `coding-policy: stateful-artifacts` + state-schema.md sibling file.
# Legacy data lacking schema_version is treated as implicit v1 (the
# field was introduced at v1; no prior version exists). Higher
# versions are forward-incompatible — return None / skip the entry.
SCHEMA_VERSION = 1


def _schema_compatible(value) -> bool:
    """Accept v1 explicitly OR legacy data with no schema_version."""
    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool) and value == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def make_slug(summary: str, start: date) -> str:
    clean = re.sub(r"\s+\d{4}$", "", summary.strip())
    slug_base = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-")
    return f"{slug_base}-{start.year}-{start.month:02d}"


def build_lodging_ranges(lodging_items: list[dict]) -> list[tuple]:
    """
    Pair 'Check-in: Hotel' and 'Check-out: Hotel' events by hotel name.
    Multiple stays at the same hotel within one trip are matched by
    replaying events per hotel in date order, where a check-out closes
    the most recently opened stay (LIFO). At the same hotel stays don't
    overlap, so the open stay is the one a check-out belongs to; LIFO
    keeps a stray earlier check-out from matching a later check-in and an
    orphan earlier check-in from stealing a later stay's check-out — both
    of which would misreport coverage. Orphan check-outs form no range;
    unmatched check-ins fall back to a 1-day range. Ranges are returned
    sorted by check-in date.
    Returns list of (checkin_date, checkout_date) tuples.
    """
    checkins: dict[str, list[date]] = {}
    checkouts: dict[str, list[date]] = {}
    for item in lodging_items:
        summary = item.get("summary", "")
        dtstart = item.get("dtstart")
        if dtstart is None:
            continue
        if summary.startswith("Check-in:"):
            hotel = summary[len("Check-in:") :].strip()
            checkins.setdefault(hotel, []).append(dtstart)
        elif summary.startswith("Check-out:"):
            hotel = summary[len("Check-out:") :].strip()
            checkouts.setdefault(hotel, []).append(dtstart)
    ranges = []
    for hotel, cis in checkins.items():
        # (date, kind): kind 0 = check-out, 1 = check-in. Sorting the
        # tuples processes a check-out before a check-in on the same day.
        events = sorted([(d, 1) for d in cis] + [(d, 0) for d in checkouts.get(hotel, [])])
        open_checkins: list[date] = []
        for d, kind in events:
            if kind == 1:
                open_checkins.append(d)
            elif open_checkins:
                ci = open_checkins.pop()
                ranges.append((ci, d) if d > ci else (ci, ci + timedelta(days=1)))
        for ci in open_checkins:
            ranges.append((ci, ci + timedelta(days=1)))
    ranges.sort()
    return ranges


def classify_trip(items: list[dict], trip_start: date, trip_end: date, today: date) -> dict:
    """Return classification flags and per-night gap list for a trip.

    `today` is injected by the caller (not read from the clock) so the
    classifier stays pure and testable. The night scan is floored at
    `today`: elapsed nights are un-bookable, so they never surface as
    gaps for a trip already underway (jbaruch/nanoclaw-travel#120).
    """
    if not items:
        return {
            "is_empty": True,
            "has_transport": False,
            "has_lodging": False,
            "uncovered_nights": [],
        }

    types = [i.get("item_type", "Unknown") for i in items]
    has_flight = "Flight" in types
    has_rail = "Rail" in types
    has_lodging = "Lodging" in types
    has_transport = has_flight or has_rail

    lodging_items = [i for i in items if i.get("item_type") == "Lodging"]
    lodging_ranges = build_lodging_ranges(lodging_items)
    uncovered_nights = []

    if has_transport:
        # Only count transport dates strictly within [trip_start, trip_end).
        # This prevents the next trip's outbound flight (included via the date-
        # overlap query) from making tail-end home-nights look like gaps.
        trip_transport_dates: set[date] = set()
        for item in items:
            if item.get("item_type") in ("Flight", "Rail"):
                for d in [item.get("dtstart"), item.get("dtend")]:
                    if d and trip_start <= d < trip_end:
                        trip_transport_dates.add(d)

        night = max(trip_start, today)
        while night < trip_end:
            covered = any(ci <= night < co for ci, co in lodging_ranges)
            is_travel_night = night in trip_transport_dates
            # No future transport = traveller is home; don't flag tail nights.
            has_future_transport = any(d > night for d in trip_transport_dates)
            if not covered and not is_travel_night and has_future_transport:
                uncovered_nights.append(night.isoformat())
            night += timedelta(days=1)

    return {
        "is_empty": False,
        "has_transport": has_transport,
        "has_lodging": has_lodging,
        "has_flight": has_flight,
        "has_rail": has_rail,
        "uncovered_nights": uncovered_nights,
    }


# ---------------------------------------------------------------------------
# Data loading: DB only
# ---------------------------------------------------------------------------


def load_trips_from_db(db_path: str) -> list[dict] | None:
    """
    Load trips from travel-db.json.
    Returns list of dicts with keys: summary, start (date), end (date), items.
    items is a list of dicts with: item_type, summary, dtstart (date), dtend (date).
    Returns None if the DB file is missing, unreadable, or structurally
    invalid — main() treats that as a hard error rather than falling
    back to a live fetch (see module docstring).
    """
    try:
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # OSError covers FileNotFoundError, PermissionError, and other
        # IO errors. UnicodeDecodeError covers a non-UTF-8 file (e.g.
        # build-travel-db.py wrote binary garbage on a half-failed
        # run). JSONDecodeError covers a partially-written or corrupt
        # DB. All three are flavors of "unreadable" and land in the
        # hard-error JSON contract in main().
        return None

    # A parseable-but-structurally-invalid root payload (db is a
    # list, or db['trips'] is a list) would crash `.items()` below
    # with AttributeError. Treat root shape errors as "unreadable"
    # too so the contract in main() holds for the full set of bad-DB
    # shapes Step 4's failure branch is meant to alert on.
    if not isinstance(db, dict) or not isinstance(db.get("trips"), dict):
        return None

    # Schema-version gate per `coding-policy: stateful-artifacts` +
    # state-schema.md sibling file. Legacy data without `schema_version`
    # is implicit v1; higher versions are forward-incompatible (treat
    # as no-prior-state).
    if not _schema_compatible(db.get("schema_version")):
        return None

    trips = []
    for slug, t in db["trips"].items():
        # Per-trip shape errors (`t` not a dict, missing required keys,
        # `days` not a dict, bad date formats, non-iterable `day_events`)
        # are caught and the trip is skipped — same fail-soft pattern
        # this loop already used for malformed dates. Skipping per-trip
        # bad data instead of failing the whole DB is the right
        # trade-off: a single malformed row from upstream ICS noise
        # would otherwise block the brief on EVERY good trip too. But
        # silent skipping hides the malformation; emit a stderr
        # diagnostic so operators can see which slugs were dropped
        # without losing the rest of the brief, per
        # `coding-policy: error-handling` (Actionable Messages) +
        # `script-delegation` (stderr diagnostics). DB-level shape
        # errors still hard-fail at the isinstance guard above.
        try:
            # `[:10]` slice tolerates the ISO-datetime shape emitted
            # for timed VEVENTs by `refresh-travel-schedule.py` after
            # `nanoclaw-admin#289` — gap-classification is day-granular,
            # so the time component is intentionally discarded here.
            trip_start = date.fromisoformat(t["start"][:10])
            trip_end = date.fromisoformat(t["end"][:10])
            summary = t["summary"]

            items = []
            # Flatten days → items list, mapping DB field names to
            # what classify_trip expects
            for day_events in t.get("days", {}).values():
                try:
                    iterator = iter(day_events)
                except TypeError:
                    # `day_events` is non-iterable (e.g. None, scalar).
                    # Skip this day; the trip's other days still parse.
                    print(
                        f"check-travel-bookings: skipped non-iterable "
                        f"day-events under trip slug={slug!r}",
                        file=sys.stderr,
                    )
                    continue
                for ev in iterator:
                    try:
                        items.append(
                            {
                                "item_type": ev["type"],
                                "summary": ev["summary"],
                                "dtstart": date.fromisoformat(ev["start"][:10]),
                                "dtend": date.fromisoformat(ev["end"][:10]),
                                "uid": ev.get("uid", ""),
                            }
                        )
                    except (KeyError, TypeError, ValueError) as ev_err:
                        print(
                            f"check-travel-bookings: skipped malformed "
                            f"item under trip slug={slug!r}: {type(ev_err).__name__}",
                            file=sys.stderr,
                        )
                        continue
        except (KeyError, TypeError, AttributeError, ValueError) as trip_err:
            print(
                f"check-travel-bookings: skipped malformed trip "
                f"slug={slug!r}: {type(trip_err).__name__}",
                file=sys.stderr,
            )
            continue

        trips.append(
            {
                "summary": summary,
                "start": trip_start,
                "end": trip_end,
                "items": items,
                "slug": slug,
            }
        )

    return trips


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _diagnose_db_failure(db_path: str) -> str:
    """Best-effort second read after `load_trips_from_db` returned None.
    Distinguishes a forward-incompatible schema_version (upgrade needed)
    from generic unreadable/missing/shape errors, so the operator
    diagnostic surfaces the actionable cause rather than a generic
    'unreadable' message that points at Step 4 in vain."""
    try:
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "missing, unreadable, or structurally invalid"
    if isinstance(db, dict):
        version = db.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
            return (
                f"has forward-incompatible schema_version={version}; "
                f"this skill supports v{SCHEMA_VERSION} — upgrade the "
                "`tessl__check-travel-bookings` plugin"
            )
    return "missing, unreadable, or structurally invalid"


def main():
    today = date.today()

    trips = load_trips_from_db(DB_PATH)
    if trips is None:
        detail = _diagnose_db_failure(DB_PATH)
        message = (
            f"travel-db.json {detail} at {DB_PATH} — "
            "tessl__nightly-travel-sync Step 4 (Rebuild "
            "travel-db.json from the schedule) should have "
            "built it. Check that step's last run in "
            "`task_run_logs` for the failure mode."
        )
        # Machine-readable JSON to stdout for the script-output
        # contract; human-readable diagnostic to stderr per
        # `coding-policy: script-delegation` (Self-error-handling)
        # and `coding-policy: file-hygiene` (stderr for diagnostics).
        print(json.dumps({"error": message}, ensure_ascii=False))
        print(f"check-travel-bookings: {message}", file=sys.stderr)
        sys.exit(1)

    # Load snooze state. The snooze file is purely advisory — a
    # missing or unreadable file means "no snoozes active", which is
    # the safe default (all gaps surface). Use the same broadened
    # except as the DB read so a permission glitch or non-UTF-8
    # write doesn't bring down the whole check.
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            snooze_state = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        snooze_state = {}
    # Valid JSON but wrong root shape (a list, a scalar, etc.) would
    # crash `.get(...)` below. Per the advisory-snooze contract, any
    # non-dict root means "no snoozes active".
    if not isinstance(snooze_state, dict):
        snooze_state = {}

    gaps = []
    complete_trips = 0

    for trip in trips:
        trip_start = trip["start"]
        trip_end = trip["end"]
        summary = trip["summary"]
        slug = trip["slug"]
        items = trip["items"]

        # Skip past trips
        if trip_end < today:
            continue

        classification = classify_trip(items, trip_start, trip_end, today)

        issue = None
        uncovered = classification.get("uncovered_nights", [])
        trip_nights = (trip_end - trip_start).days
        # A transport-only trip with no lodging needs a hotel unless the
        # traveller is still in transit at the end of the trip window: a
        # same-day round trip (whose return arrival often slips past UTC
        # midnight) or a red-eye lands at or after trip_end, so no night
        # is spent staying at a destination. When the latest transport
        # arrival within the trip falls before trip_end, the traveller
        # has landed and is staying over — a missing hotel is a real gap
        # even when the lone travel night leaves uncovered empty. A
        # zero-night day trip needs no hotel at all.
        trip_arrivals = [
            i["dtend"]
            for i in items
            if i.get("item_type") in ("Flight", "Rail")
            and i.get("dtstart") is not None
            and i.get("dtend") is not None
            and trip_start <= i["dtstart"] < trip_end
        ]
        in_transit_through_end = bool(trip_arrivals) and max(trip_arrivals) >= trip_end
        trip_needs_lodging = trip_nights >= 1 and not in_transit_through_end
        if classification["is_empty"]:
            issue = "ничего не забукано"
        elif (
            classification["has_transport"]
            and not classification["has_lodging"]
            and trip_needs_lodging
        ):
            issue = "рейсы есть, отеля нет"
        elif classification["has_transport"] and uncovered:
            issue = f"нет отеля на {len(uncovered)} ноч.: {uncovered[0]}…{uncovered[-1]}"

        if issue is None:
            complete_trips += 1
            continue

        # Check snooze. Per-entry schema_version gate per state-schema.md:
        # entries with a higher-than-current schema_version are treated as
        # forward-incompatible (no snooze active). Missing schema_version is
        # legacy data, accepted as implicit v1. Non-dict entries are
        # malformed → no snooze active.
        snooze_entry = snooze_state.get(slug, {})
        if not isinstance(snooze_entry, dict) or not _schema_compatible(
            snooze_entry.get("schema_version")
        ):
            snooze_entry = {}
        snooze_until_str = snooze_entry.get("snooze_until", "")
        if snooze_until_str:
            try:
                if date.fromisoformat(snooze_until_str) >= today:
                    complete_trips += 1
                    continue
            except ValueError:
                pass

        gaps.append(
            {
                "trip": summary,
                "start": trip_start.isoformat(),
                "end": trip_end.isoformat(),
                "issue": issue,
                "slug": slug,
                "uncovered_nights": uncovered if uncovered else [],
            }
        )

    output = {
        "gaps": gaps,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_trips": len(trips),
        "complete_trips": complete_trips,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
