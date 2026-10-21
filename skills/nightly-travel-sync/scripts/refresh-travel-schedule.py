import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

# travel-core owns the `Check-in:` / `Check-out:` discriminator this writer
# stamps and every downstream reader matches on. Runtime mount first, dev-clone
# sibling fallback for CI (travel-core's SKILL.md pattern; this script sits one
# level deeper, under `scripts/`).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

_BUNDLE_DIR = _SCRIPT_DIR.parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from lodging import hotel_name, lodging_role  # noqa: E402

# Sibling module, resolved through the `_SCRIPT_DIR` insert above so the
# import holds whether this file is executed or loaded by path (drive-engine's
# `reconcile_sweep` uses the same shape). It reconstructs the traveller's own
# clock from the UTC instant this feed stamps plus the local time DESCRIPTION
# prints — see `tripit_local_time.py`.
from tripit_local_time import local_times  # noqa: E402

TIMEOUT_SECONDS = 30
URL_PATH = "/workspace/group/tripit-url.txt"
OUTPUT_PATH = "/workspace/group/travel-schedule.json"

# Per `jbaruch/coding-policy: stateful-artifacts`, every record carries a
# schema_version. travel-schedule.json is a list of event records;
# the version lives on each record because the artifact is regenerated
# in full from the live TripIt ICS every run (no in-place migration —
# the writer always emits the current version, cross-plugin readers gate
# on it). See the sibling `state-schema.md` for the owner/reader contract.
SCHEMA_VERSION = 5

# RFC 5545 §3.3.11 TEXT escapes, as the feed writes them: `San Francisco\, CA`.
# One pass over the string so an escaped backslash (`\\,`) yields a literal
# backslash followed by a comma rather than being re-read as an escaped comma.
_ICS_ESCAPE_RE = re.compile(r"\\(.)")
_ICS_ESCAPES = {"n": "\n", "N": "\n"}


def _unescape_ics_text(value):
    """An ICS TEXT value with its escapes unwound (`Nashville\\, TN`).

    An unknown escape yields the escaped character itself, which is what the
    spec's `\\;` `\\,` `\\\\` set needs and the safest reading of anything else
    TripIt emits — never a dropped character.
    """
    return _ICS_ESCAPE_RE.sub(lambda m: _ICS_ESCAPES.get(m.group(1), m.group(1)), value)


def lodging_pair_key(summary, description):
    """For a Lodging VEVENT, return (role, key) where role is 'in' or
    'out' and key is the (trip_id, hotel) pair that ties a check-in to
    its check-out. Returns (None, None) when the summary isn't a
    `Check-in:`/`Check-out:` line, OR when no `trip/show/id/<n>` URL can
    be parsed from DESCRIPTION.

    TripIt models a stay as two VEVENTs — `Check-in: <hotel>` and
    `Check-out: <hotel>` — both carrying the same trip URL in DESCRIPTION
    and the same LOCATION. We pair them by trip-ID (parsed from the
    `trip/show/id/<n>` URL) + hotel name, mirroring the pairing
    `check-travel-bookings.py:build_lodging_ranges()` does downstream.

    Pairing requires BOTH the trip-ID and the hotel name. We deliberately
    do NOT fall back to hotel-name-only matching when the trip URL is
    missing: a `None` trip-ID would let a future check-out at a
    same-named hotel in a *different* trip rescue an unrelated past
    check-in, masking a real lodging gap (a silent false-negative). With
    no trip-ID the event simply isn't paired and falls back to the
    ordinary `end < now` filter, so the worst case is the visible #41
    false-gap alert reappearing rather than a hidden missed gap.
    """
    role = lodging_role(summary)
    if role is None:
        return None, None
    m = re.search(r"trip/show/id/(\d+)", description)
    if not m:
        return None, None
    hotel = hotel_name(summary)
    if hotel is None:
        return None, None
    return role, (m.group(1), hotel)


def _get_field(component, field):
    """Extract a single ICS field value from a VEVENT component."""
    m = re.search(rf"^{field}[^:\r\n]*:(.+)", component, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _parse_dt(s):
    """Parse an ICS datetime, returning `(datetime|None, had_time)`.

    `strptime` documents `ValueError` for bad-format / out-of-range
    input; a bare `except` would also swallow programmer errors. Try the
    full datetime form first — timed VEVENTs (flights, lodging check-ins,
    rentals) arrive as `YYYYMMDDTHHMMSSZ` and need their time-of-day
    preserved so the downstream `end < now` filter doesn't drop today's
    events once UTC midnight has rolled over. Date-only form
    (`DTSTART;VALUE=DATE:YYYYMMDD`, used by trip-level VEVENTs) remains
    the fallback.

    `had_time` is False on both the date-only branch and the
    parse-failure branch, so the emit step preserves time-of-day on
    timed events and emits date-only on `VALUE=DATE` events.
    """
    s = s.strip()
    try:
        return (
            datetime.strptime(s.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=UTC),
            True,
        )
    except ValueError:
        pass
    try:
        return datetime.strptime(s, "%Y%m%d").replace(tzinfo=UTC), False
    except ValueError:
        return None, False


def validate_schedule(records):
    """Reject incomplete or conflicting cache evidence before atomic replacement."""
    error = (
        "Invalid TripIt schedule; verify the feed and retry; previous cache retained."
    )
    if not isinstance(records, list):
        raise RuntimeError(error)
    seen = set()
    for record in records:
        if (
            not isinstance(record, dict)
            or type(record.get("schema_version")) is not int
        ):
            raise RuntimeError(error)
        if record["schema_version"] != SCHEMA_VERSION:
            raise RuntimeError(error)
        for field in ("uid", "type", "summary", "start", "end"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(error)
        uid = record["uid"]
        if uid in seen:
            raise RuntimeError(error)
        seen.add(uid)
        values = []
        for field in ("start", "end"):
            value = record[field]
            fmt = "%Y-%m-%d" if len(value) == 10 else "%Y-%m-%dT%H:%M:%SZ"
            try:
                instant = datetime.strptime(value, fmt).replace(tzinfo=UTC)
            except ValueError as exc:
                raise RuntimeError(error) from exc
            if instant.strftime(fmt) != value:
                raise RuntimeError(error)
            values.append(instant)
        if values[0] > values[1]:
            raise RuntimeError(error)


def write_schedule(events):
    """Validate the actual staged bytes and retain the previous cache on failure."""
    try:
        descriptor, tmp_path = tempfile.mkstemp(
            prefix=Path(OUTPUT_PATH).name + ".",
            suffix=".tmp",
            dir=Path(OUTPUT_PATH).parent,
        )
    except OSError as exc:
        raise RuntimeError(
            "Cannot stage TripIt schedule; check output permissions and disk space, "
            "then retry; previous cache retained."
        ) from exc
    try:
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(events, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            try:
                with open(tmp_path) as staged:
                    verified = json.load(staged)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "Invalid staged TripIt JSON; retry the refresh; previous cache retained."
                ) from exc
            validate_schedule(verified)
            os.replace(tmp_path, OUTPUT_PATH)
            return verified
        except OSError as exc:
            raise RuntimeError(
                "Cannot replace TripIt schedule; check output permissions and disk space, "
                "then retry; previous cache retained."
            ) from exc
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass  # Successful replacement already consumed this invocation's file.


def main():
    with open(URL_PATH) as f:
        url = f.read().strip()
    if not url:
        raise RuntimeError(f"TripIt URL file is empty: {URL_PATH}")

    # Fetch with one retry on transient failure. Timeout is explicit so a flaky
    # TripIt endpoint can't block the nightly task indefinitely. Use a context
    # manager so the urllib response object is always closed, even if `.read()`
    # or the utf-8 decode raises — relevant in a retry loop where leaked
    # descriptors would compound across attempts.
    ics = None
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
                ics = response.read().decode("utf-8")
            break
        # Narrow per `jbaruch/coding-policy: error-handling`. The expected
        # transient failures are the urllib network family + the timeout
        # families urlopen surfaces under the explicit `timeout=`, plus
        # any decode failure from a feed that shipped non-UTF-8 bytes;
        # programmer errors propagate.
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError) as e:
            last_error = e
            if attempt == 0:
                time.sleep(5)

    if ics is None:
        raise RuntimeError(f"ICS fetch failed after 2 attempts: {last_error}")

    # Unfold ICS line continuations (RFC 5545: CRLF + whitespace = continuation)
    ics = re.sub(r"\r?\n[ \t]", "", ics)

    if not ics.strip().startswith("BEGIN:VCALENDAR") or not ics.strip().endswith(
        "END:VCALENDAR"
    ):
        raise RuntimeError(
            "Invalid TripIt calendar; verify the feed and retry; previous cache retained."
        )

    now = datetime.now(UTC)

    # First pass: parse every VEVENT into a record. The past-event filter
    # is deferred to a second pass so Lodging events can be paired before
    # anything is dropped. TripIt models a stay as two 1-hour VEVENTs —
    # `Check-in:` and `Check-out:` — and the check-in's DTEND is one hour
    # after check-in, i.e. already past while you're still in the room.
    # The old per-event `end < now` filter dropped that check-in, which
    # orphaned the check-out and made check-travel-bookings.py report a
    # false "no hotel for N nights" gap (issue #41).
    parsed = []
    for component in ics.split("BEGIN:VEVENT")[1:]:
        start, start_timed = _parse_dt(_get_field(component, "DTSTART"))
        end, end_timed = _parse_dt(_get_field(component, "DTEND"))
        if not start or not end:
            raise RuntimeError(
                "Invalid TripIt event dates; verify the feed and retry; previous cache retained."
            )

        uid = _get_field(component, "UID")
        description = _get_field(component, "DESCRIPTION")

        # Determine type from DESCRIPTION [Type] marker or UID
        event_type = "Unknown"
        if "item-" not in uid:
            event_type = "Trip"
        else:
            # Parse [Type] from DESCRIPTION field (appears as e.g. "[Flight] ATL to SJO")
            type_match = re.search(r"\[([^\]]+)\]", description)
            if type_match:
                raw_type = type_match.group(1)
                # Pass raw_type through unchanged — the old ternary
                # `raw_type if raw_type in known_types else raw_type` was
                # dead code (both branches identical) and the known_types
                # list was defined but never used to filter anything. Keep
                # the raw value so novel item types (e.g. TripIt adds a new
                # category) don't silently become 'Unknown' — downstream
                # build-travel-db.py sorts by TYPE_ORDER with a default
                # ordinal for unknowns, which is the right place to handle
                # drift.
                event_type = raw_type

        parsed.append(
            {
                # v5: decoded, matching `location`. The feed escapes
                # commas here too (`Check-in: Radisson Blu Airport
                # Hotel\\, Oslo`), and the string is operator-visible —
                # the booking brief and the drive-engine diagnostics
                # print it as-is (#278).
                #
                # Safe to decode at the writer because every parse key
                # derived from it is unchanged by the escape:
                #   * `trip_key` slugs via `[^a-z0-9]+`, which collapses
                #     the backslash and the comma into the same `-`, so
                #     slugs are byte-identical either way and no stored
                #     per-trip drive verdict is orphaned;
                #   * `lodging_pair_key` parses both the check-in and
                #     the check-out from summaries decoded the same way,
                #     so a stay's two halves still pair;
                #   * `flight_summaries` reads IATA designators, which
                #     carry no TEXT escapes to unwind.
                # All three are pinned by tests rather than assumed.
                "summary": _unescape_ics_text(_get_field(component, "SUMMARY")),
                "start": start,
                "start_timed": start_timed,
                "end": end,
                "end_timed": end_timed,
                # v4: decoded, not verbatim. `travel-core`'s `resolve_anchor`
                # hands this string out as a drivable address, so the feed's
                # transport escapes were riding into the geocode request
                # (`San Francisco\, CA`) — see the sibling state-schema.md.
                "location": _unescape_ics_text(_get_field(component, "LOCATION")),
                "type": event_type,
                "uid": uid,
                "description": description,
            }
        )

    # Collect the pairing keys of Lodging stays that are still live —
    # in progress or upcoming — keyed by (trip_id, hotel). A check-out's
    # DTEND stays in the future until the actual check-out instant, so an
    # unexpired check-out marks a live stay whose check-in must be kept
    # even after the check-in hour has passed (issue #41).
    live_lodging = set()
    for ev in parsed:
        if ev["type"] == "Lodging" and ev["end"] >= now:
            role, key = lodging_pair_key(ev["summary"], ev["description"])
            if role == "out" and key is not None:
                live_lodging.add(key)

    # Second pass: drop past events, but keep a past Lodging check-in
    # whose matching check-out is still live so downstream pairing in
    # check-travel-bookings.py can reconstruct the full stay span.
    events = []
    for ev in parsed:
        keep = ev["end"] >= now
        if not keep and ev["type"] == "Lodging":
            role, key = lodging_pair_key(ev["summary"], ev["description"])
            if role == "in" and key in live_lodging:
                keep = True
        if not keep:
            continue

        # Preserve time-of-day for timed VEVENTs (flights, lodging
        # check-ins, rentals) so downstream consumers can answer
        # "what time does today's flight depart?" without a calendar
        # round-trip. Date-only VEVENTs (trip-level wrappers) stay
        # `YYYY-MM-DD` — emitting midnight on those would invent a
        # precision the source feed doesn't have.
        start_iso = (
            ev["start"].strftime("%Y-%m-%dT%H:%M:%SZ")
            if ev["start_timed"]
            else ev["start"].strftime("%Y-%m-%d")
        )
        end_iso = (
            ev["end"].strftime("%Y-%m-%dT%H:%M:%SZ")
            if ev["end_timed"]
            else ev["end"].strftime("%Y-%m-%d")
        )

        record = {
            "schema_version": SCHEMA_VERSION,
            "summary": ev["summary"],
            "start": start_iso,
            "end": end_iso,
            "location": ev["location"],
            "type": ev["type"],
            "uid": ev["uid"],
            # v2: persist DESCRIPTION — the iCal `[Type] <DEP> to <ARR>` line
            # is the only reliable source of a Flight segment's route (both
            # airports), consumed by drive-engine's TripIt-union parser (#156
            # R2). Additive; readers that don't use it are unaffected.
            "description": ev["description"],
        }

        # v3: the traveller's own clock, offset included. The feed stamps
        # UTC only, so a night spent on a red-eye out of San Francisco was
        # filed under the next day and read as a night owed a hotel (#268).
        # Additive and omitted when unresolved (see `tripit_local_time`),
        # so a reader that doesn't ask for it, and a record that can't
        # answer, both stay on the UTC dates they used at v2.
        if ev["start_timed"] and ev["end_timed"]:
            local_start, local_end = local_times(
                ev["start"], ev["end"], ev["description"]
            )
            if local_start is not None:
                record["start_local"] = local_start.isoformat()
            if local_end is not None:
                record["end_local"] = local_end.isoformat()

        events.append(record)

    events.sort(key=lambda e: e["start"])

    # Atomic write per `coding-policy: stateful-artifacts`: stage a
    # same-dir `.tmp` sibling, validate it, then `os.replace` it into
    # place. A failed or partial refresh never truncates or freshens the
    # live travel-schedule.json — the mtime-preserving failure contract
    # the sibling state-schema.md documents. Mirrors build-travel-db.py.
    verified = write_schedule(events)

    # Structured JSON run summary per `jbaruch/coding-policy:
    # script-delegation` (Script Requirements: JSON-producing — output
    # structured data, not prose). The durable artifact is
    # travel-schedule.json (written above); this single stdout line is
    # the operator-facing signal in nightly logs.
    type_counts = Counter(e["type"] for e in verified)
    print(
        json.dumps(
            {
                "events_written": len(events),
                "type_breakdown": dict(type_counts),
                "sample": [
                    {"type": e["type"], "summary": e["summary"], "start": e["start"]}
                    for e in verified[:15]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    # Self-error-handling per `jbaruch/coding-policy: script-delegation`:
    # the expected operational failures — a missing or empty
    # `tripit-url.txt`, or an ICS fetch that fails both attempts — exit
    # non-zero with a single clean stderr line (not a traceback) so
    # SKILL.md Step 2 can surface it verbatim. Programmer/data-shape
    # faults (e.g. the post-write validation asserts) still propagate.
    try:
        main()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"refresh-travel-schedule: {exc}", file=sys.stderr)
        sys.exit(1)
