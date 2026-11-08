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

Trips to the operator's own metro are skipped outright — see the home-metro
guard in `main()`.
"""

import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# travel-core owns the `Check-in:` / `Check-out:` discriminator. Runtime mount
# first, dev-clone sibling fallback for CI (travel-core's SKILL.md pattern; this
# script sits one level deeper, under `scripts/`).
_BUNDLE_DIR = Path(__file__).resolve().parent.parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))
_DRIVE_ENGINE = Path("/home/node/.claude/skills/tessl__drive-engine")
if not _DRIVE_ENGINE.is_dir():
    _DRIVE_ENGINE = _BUNDLE_DIR.parent / "drive-engine"
if str(_DRIVE_ENGINE) not in sys.path:
    sys.path.insert(0, str(_DRIVE_ENGINE))

from addresses import home_metro_names, is_home_metro  # noqa: E402
from lodging import CHECK_IN, CHECK_OUT, hotel_name, lodging_role  # noqa: E402

DB_PATH = "/workspace/group/travel-db.json"
STATE_PATH = "/workspace/group/travel-booking-state.json"

# drive-engine's drive-or-fly verdict store, read here READ-ONLY and
# non-migrating. Owner and full contract: `skills/drive-engine/drive_decision.py`
# + `skills/drive-engine/state-schema.md`. The directory keeps the historical
# `drive-planner` name and its env override so both skills resolve one path.
DRIVE_DECISIONS_DIR_ENV = "DRIVE_PLANNER_STATE_DIR"
DRIVE_DECISIONS_DEFAULT_DIR = "/workspace/state/drive-planner"
DRIVE_DECISIONS_FILE = "drive-decisions.json"
DRIVE_DECISIONS_SCHEMA_VERSION = 2

# The verdicts that make a flight-less trip a booking gap.
#   fly     — the operator is flying (or the drive is too long to be one) and
#             no flight is booked. A settled gap.
#   unknown — the drive time is ambiguous and the engine is still waiting on
#             an answer. Also surfaced, worded as a question, because a
#             question asked once and missed is a question lost: the engine
#             stamps `asked_at` and never asks again, so nothing re-raised it
#             and the trip got no drive legs AND no alert (#240). This check
#             runs daily, which is the right cadence for a nudge — the 30-min
#             sweep would be a nag.
# `drive` is not a gap: the engine is planning the drive, no flight expected.
VERDICT_FLY = "fly"
VERDICT_UNKNOWN = "unknown"
_GAP_VERDICTS = frozenset({VERDICT_FLY, VERDICT_UNKNOWN})

# The two wordings of the hotel-without-transport gap. Relayed VERBATIM into
# the operator-facing line (`SKILL.md` Step 3), so the `drive` / `fly` reply
# words the drive engine's answer path matches are named in the ask itself.
TRANSPORT_GAP_ISSUE = "отель есть, рейса нет"
TRANSPORT_GAP_ASK_ISSUE = "отель есть, рейса нет — едешь на машине? ответь «drive» или «fly»"

# Bump in lock-step with build-travel-db.py per
# `coding-policy: stateful-artifacts` + state-schema.md sibling file.
# Legacy data lacking schema_version is treated as implicit v1 (the
# field was introduced at v1; no prior version exists). Higher
# versions are forward-incompatible — return None / skip the entry.
SCHEMA_VERSION = 3

# Every bump so far has been additive — v2 adds optional `start_local`/
# `end_local` to day items, v3 an optional trip-level `destination` — so an
# older DB reads with the newer fields simply absent. Accepting the whole set
# keeps each rollout zero-skew per `coding-policy: stateful-artifacts`: the
# on-disk DB stays at the old version until the next nightly rebuild, and a
# current-version-only reader would hard-error the whole brief in between. The
# same set gates the snooze store, whose entries carry their own stamp and
# whose shape has not changed.
_ACCEPTED_SCHEMA_VERSIONS = frozenset({1, 2, SCHEMA_VERSION})


def _schema_compatible(value) -> bool:
    """Accept an accepted version explicitly OR legacy data with no schema_version."""
    if value is None:
        return True
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value in _ACCEPTED_SCHEMA_VERSIONS
    )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _still_active(record: dict, now: datetime) -> bool:
    """Whether a verdict record is still in force at `now`.

    `expires` is required by the writer's contract (`state-schema.md`), so a
    record missing it, carrying an unparseable or naive one, or already past is
    not a verdict — it is residue. Honouring it here rather than trusting the
    owner's prune is what keeps a finished trip from nagging: the owner drops
    expired records on its own sweep, but a reader that depends on another
    skill's housekeeping having run is a reader that reports stale state.
    """
    raw = record.get("expires")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return expires.tzinfo is not None and expires > now


def load_transport_gap_verdicts(path: Path | None = None, *, now: datetime | None = None) -> dict:
    """Trip slug → drive-or-fly verdict, for the verdicts that are a booking gap.

    A READ-ONLY, non-migrating consumer of another skill's artifact
    (`coding-policy: stateful-artifacts`). Deliberately looser than the owner's
    own reader: a missing, unreadable, non-object, or unrecognized-version file
    yields an EMPTY MAP, never an exception. The no-prior-state path has to stay
    non-disruptive, and inventing missing-flight alerts out of an unreadable
    file is exactly the alert storm that rule forbids — under-reporting a gap is
    recoverable, a storm of false ones is not.

    Returns `fly` and `unknown` verdicts (see `_GAP_VERDICTS`) that are still
    active at `now` (defaults to the current UTC instant); `drive` is omitted,
    since the engine is planning the drive and no flight is expected. An
    expired record is dropped — see `_still_active`.
    The caller words the two differently — `fly` is a settled gap, `unknown` is
    the daily nudge that replaces a one-shot question nobody re-asked.
    """
    records = _active_drive_decisions(path, now=now)
    return {
        slug: record["verdict"]
        for slug, record in records.items()
        if record.get("verdict") in _GAP_VERDICTS and not slug.startswith("meeting:")
    }


def _active_drive_decisions(path: Path | None = None, *, now: datetime | None = None) -> dict:
    """Read the current owner version without migrating or writing its file."""
    target = (
        path
        or Path(os.environ.get(DRIVE_DECISIONS_DIR_ENV, DRIVE_DECISIONS_DEFAULT_DIR))
        / DRIVE_DECISIONS_FILE
    )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    version = payload.get("schema_version")
    if type(version) is not int or version != DRIVE_DECISIONS_SCHEMA_VERSION:
        return {}
    trips = payload.get("trips")
    if not isinstance(trips, dict):
        return {}
    at = now if now is not None else datetime.now(UTC)
    return {
        slug: record
        for slug, record in trips.items()
        if isinstance(slug, str) and isinstance(record, dict) and _still_active(record, at)
    }


class MeetingGapUnavailable(RuntimeError):
    """The report cannot verify a recalled meeting against its current sources."""


def _meeting_gap_sources(*, now, until):
    """Reuse the owner clients; no cache refresh or drive sweep is applied."""
    from fetch_events import CalendarFetcher, GoogleCalendarError
    from maps_client import MapsClient
    from route_lookup import make_route
    from tripit_feed import fetch_schedule

    try:
        events = CalendarFetcher().fetch_window(time_min=now, time_max=until)
        # A just-arrived flight still covers an upcoming meeting. The nightly
        # writer's usual future-only filter would otherwise hide that booking.
        schedule = fetch_schedule(now=now - timedelta(days=2))
        route = make_route(MapsClient.from_env())
    except (GoogleCalendarError, OSError, RuntimeError, ValueError, UnicodeError) as exc:
        # Never echo feed URLs or upstream response bodies into the brief.
        raise MeetingGapUnavailable(
            "Meeting booking gaps unavailable; check Calendar, TripIt feed and Maps "
            "access, then rerun check-travel-bookings."
        ) from exc
    return events, schedule, route


def load_meeting_transport_gaps(
    path: Path | None = None,
    *,
    now: datetime | None = None,
    events=None,
    schedule=None,
    route=None,
) -> tuple[list, str | None]:
    """Verify recalled long journeys against current Calendar and TripIt evidence."""
    from itinerary import Itinerary
    from meeting_travel import booked_transport_to
    from scan import _is_virtual, _parse_event

    at = now if now is not None else datetime.now(UTC)
    candidates = []
    for key, record in _active_drive_decisions(path, now=at).items():
        meeting = record.get("meeting")
        if not key.startswith("meeting:") or not isinstance(meeting, dict):
            continue
        if not all(
            isinstance(meeting.get(k), str) and meeting[k]
            for k in ("summary", "start", "end", "destination")
        ):
            continue
        try:
            start = datetime.fromisoformat(meeting["start"])
            end = datetime.fromisoformat(meeting["end"])
        except ValueError:
            continue
        if start.tzinfo is None or end.tzinfo is None or end <= at:
            continue
        issue = meeting.get("gap_reason")
        if not issue and record.get("verdict") == VERDICT_FLY:
            issue = "нужен рейс или поезд к встрече — брони в TripIt нет"
        if isinstance(issue, str) and issue:
            candidates.append(
                {
                    "trip": meeting["summary"],
                    "start": start.date().isoformat(),
                    "end": end.date().isoformat(),
                    "issue": issue,
                    "slug": key,
                    "uncovered_nights": [],
                    "meeting_start": start,
                    "meeting_end": end,
                    "destination": meeting["destination"],
                }
            )
    if not candidates:
        return [], None
    if events is None or schedule is None or route is None:
        events, schedule, route = _meeting_gap_sources(
            now=at, until=max(gap["meeting_end"] for gap in candidates)
        )
    current = {event.raw_id: event for raw in events if (event := _parse_event(raw)).raw_id}
    itinerary = Itinerary(schedule, home_address=None)
    gaps = []
    unavailable = None
    for gap in candidates:
        event = current.get(gap["slug"].removeprefix("meeting:"))
        if (
            event is None
            or event.cancelled
            or event.declined
            or _is_virtual(event.location)
            or event.start is None
            or event.end is None
            or event.location is None
            or event.start != gap["meeting_start"]
            or event.end != gap["meeting_end"]
            or event.location != gap["destination"]
        ):
            continue  # Changed meetings need the owner's next live route calculation.
        booked = booked_transport_to(
            destination=event.location,
            at=event.start,
            until=event.end,
            schedule=schedule,
            itinerary=itinerary,
            route=route,
        )
        if booked is None:
            unavailable = (
                "Meeting booking gaps unavailable; verify the booked arrival station/airport "
                "and Maps access, then rerun check-travel-bookings."
            )
            continue
        if not booked:
            gaps.append(
                {
                    k: v
                    for k, v in gap.items()
                    if k not in {"meeting_start", "meeting_end", "destination"}
                }
                | {"trip": event.summary}
            )
    return gaps, unavailable


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
        role = lodging_role(summary)
        hotel = hotel_name(summary)
        if hotel is None:
            continue
        if role == CHECK_IN:
            checkins.setdefault(hotel, []).append(dtstart)
        elif role == CHECK_OUT:
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

    `has_bookable_window` carries the same "is there anything left to
    book" question for the whole trip rather than a single night, and is
    what the empty-itinerary alert gates on (#286). `is_empty` stays a
    truthful statement about the itinerary — the trip really does have
    zero items — so the two are reported separately rather than
    collapsing a departed trip into "not empty".
    """
    has_bookable_window = today < trip_start
    if not items:
        return {
            "is_empty": True,
            "has_bookable_window": has_bookable_window,
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
        "has_bookable_window": has_bookable_window,
        "has_transport": has_transport,
        "has_lodging": has_lodging,
        "has_flight": has_flight,
        "has_rail": has_rail,
        "uncovered_nights": uncovered_nights,
    }


# ---------------------------------------------------------------------------
# Data loading: DB only
# ---------------------------------------------------------------------------


def _item_day(event: dict, field: str) -> date:
    """The traveller's calendar date for a day item's `start` or `end`.

    Prefers the v2 `<field>_local` stamp — the same instant on the clock the
    traveller was reading — and falls back to the UTC `<field>` a v1 DB
    carries alone. A red-eye leaving San Francisco at 11:05 PM is an 06:05Z
    event the next day, so the UTC fallback books that departure a day late
    and the night it covers reads as a night owed a hotel (#268).

    `[:10]` slices the date off either shape: `YYYY-MM-DD`, the UTC
    `YYYY-MM-DDTHH:MM:SSZ`, and the local `YYYY-MM-DDTHH:MM:SS±HH:MM` all
    carry it in the same leading ten characters. Gap classification is
    day-granular, so the time is intentionally discarded. Raises the same
    `KeyError`/`TypeError`/`ValueError` the caller already catches per item.
    """
    local = event.get(f"{field}_local")
    raw = local if isinstance(local, str) and local else event[field]
    return date.fromisoformat(raw[:10])


def load_trips_from_db(db_path: str) -> list[dict] | None:
    """
    Load trips from travel-db.json.
    Returns list of dicts with keys: summary, start (date), end (date),
    destination (str, "" when the DB carries none), items.
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
                                "dtstart": _item_day(ev, "start"),
                                "dtend": _item_day(ev, "end"),
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

        # v3, optional: absent on a pre-v3 DB and on a trip the feed left
        # unlabelled. Normalized to "" so the caller's home-metro test reads
        # one shape, and an unknown destination is never home.
        destination = t.get("destination")
        trips.append(
            {
                "summary": summary,
                "start": trip_start,
                "end": trip_end,
                "destination": destination if isinstance(destination, str) else "",
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

    # The drive engine's verdict per flight-less trip, for the two that are a
    # gap (#231, #240). Empty when the store is absent or unreadable — see
    # `load_transport_gap_verdicts`.
    transport_verdicts = load_transport_gap_verdicts()

    # The operator's home metro, read from the trusted profile's canonical
    # `## Addresses` block (see `skills/travel-core/addresses.py`). Empty when the key
    # is unset, which is the pre-#271 behaviour: every trip gets checked.
    home_metro = home_metro_names()

    gaps = []
    complete_trips = 0
    local_trips = 0

    for trip in trips:
        trip_start = trip["start"]
        trip_end = trip["end"]
        summary = trip["summary"]
        slug = trip["slug"]
        items = trip["items"]

        # Skip past trips
        if trip_end < today:
            continue

        # Skip trips to the operator's own metro. These are placeholders filed
        # to block time for a local event — a home game, a family surgery — so
        # byAir, drive-engine, and cfp-conflict-check see the day is taken.
        # There is no flight and no hotel to book at home, so an empty
        # itinerary here is the finished state, not a gap (#271). The empty
        # itinerary alone can NOT decide this: an away trip with nothing booked
        # yet looks identical and is exactly what this check exists to catch.
        # The destination is the separator, which is why it rides in the DB.
        if is_home_metro(trip["destination"], home_metro):
            local_trips += 1
            continue

        classification = classify_trip(items, trip_start, trip_end, today)

        issue = None
        uncovered = classification.get("uncovered_nights", [])
        trip_nights = (trip_end - trip_start).days
        # A transport-only trip with no lodging needs a hotel unless the
        # traveller never stops travelling: a same-day round trip runs past
        # the window's end, so no night is spent staying at a destination.
        # An arrival that lands before the end means they arrived and are
        # staying over — a real gap even when the lone travel night leaves
        # uncovered empty. A zero-night day trip needs no hotel.
        trip_arrivals = [
            i["dtend"]
            for i in items
            if i.get("item_type") in ("Flight", "Rail")
            and i.get("dtstart") is not None
            and i.get("dtend") is not None
            and trip_start <= i["dtstart"] < trip_end
        ]
        # A red-eye home lands inside the window rather than past it, so the
        # arrival date alone reads it as "arrived and staying over" and a
        # hotel-free turnaround got flagged as a missing booking (#268). The
        # overnight span is what marks it: a segment that leaves one day and
        # lands on the trip's final day was the last night, spent in the air.
        # Only the local dates make that visible — in UTC the departure and
        # the arrival collapse onto the same calendar day.
        trip_last_day = trip_end - timedelta(days=1)
        red_eye_home = any(
            i["dtend"] == trip_last_day and i["dtstart"] < i["dtend"]
            for i in items
            if i.get("item_type") in ("Flight", "Rail")
            and i.get("dtstart") is not None
            and i.get("dtend") is not None
        )
        in_transit_through_end = bool(trip_arrivals) and (
            max(trip_arrivals) >= trip_end or red_eye_home
        )
        # A night the scan already found uncovered settles it: the traveller
        # is on the ground somewhere with no bed booked, and how the trip
        # ENDS cannot excuse a night in the middle of it. Only a trip with no
        # uncovered night at all leans on the in-transit test.
        trip_needs_lodging = trip_nights >= 1 and (bool(uncovered) or not in_transit_through_end)
        # #286: a trip that has already started has no bookable window left,
        # so "nothing booked" is noise rather than a prompt — the traveller is
        # at the destination, having booked out of band or not at all. A FUTURE
        # empty away-trip is still the signal this check exists for (#271).
        if classification["is_empty"] and classification["has_bookable_window"]:
            issue = "ничего не забукано"
        elif (
            classification["has_transport"]
            and not classification["has_lodging"]
            and trip_needs_lodging
        ):
            issue = "рейсы есть, отеля нет"
        elif classification["has_transport"] and uncovered:
            issue = f"нет отеля на {len(uncovered)} ноч.: {uncovered[0]}…{uncovered[-1]}"
        elif (
            not classification["has_transport"]
            and classification["has_lodging"]
            and slug in transport_verdicts
        ):
            # The mirror of "рейсы есть, отеля нет": hotel booked, nothing to
            # get there on. Gated on the drive engine's verdict rather than on
            # the missing transport alone, because a trip the operator drives to
            # has no transport booking by design and alerting on it would nag
            # about every weekend away (#231).
            #
            # An `unknown` verdict is the same gap with the question still open,
            # so it is worded as one. This daily surface is what makes the
            # question survivable: the engine asks once and never re-asks, so a
            # missed notice used to leave the trip with no drive legs and no
            # alert at all (#240). Answering either way clears it — `drive`
            # drops out of `_GAP_VERDICTS`, `fly` settles into the line above.
            issue = (
                TRANSPORT_GAP_ISSUE
                if transport_verdicts[slug] == VERDICT_FLY
                else TRANSPORT_GAP_ASK_ISSUE
            )

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

    meeting_error = None
    try:
        meeting_gaps, meeting_error = load_meeting_transport_gaps()
    except MeetingGapUnavailable as exc:
        meeting_error = str(exc)
        meeting_gaps = []
    if meeting_error is not None:
        print(f"check-travel-bookings: {meeting_error}", file=sys.stderr)
    for gap in meeting_gaps:
        entry = snooze_state.get(gap["slug"], {})
        until = entry.get("snooze_until") if isinstance(entry, dict) else None
        if isinstance(until, str) and _schema_compatible(entry.get("schema_version")):
            try:
                if date.fromisoformat(until) >= today:
                    continue
            except ValueError:
                pass
        gaps.append(gap)

    output = {
        "gaps": gaps,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_trips": len(trips),
        "complete_trips": complete_trips,
        # Home-metro placeholders, counted rather than folded into
        # `complete_trips`: nothing about them was checked, and an operator
        # asking "why didn't it flag that one?" reads the answer here.
        "local_trips": local_trips,
    }
    if meeting_error is not None:
        output["meeting_gaps_unavailable"] = meeting_error
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
