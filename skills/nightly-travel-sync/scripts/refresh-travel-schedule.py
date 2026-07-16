import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

TIMEOUT_SECONDS = 30
URL_PATH = "/workspace/group/tripit-url.txt"
OUTPUT_PATH = "/workspace/group/travel-schedule.json"

# Per `jbaruch/coding-policy: stateful-artifacts`, every record carries a
# schema_version. travel-schedule.json is a list of event records;
# the version lives on each record because the artifact is regenerated
# in full from the live TripIt ICS every run (no in-place migration —
# the writer always emits the current version, cross-plugin readers gate
# on it). See the sibling `state-schema.md` for the owner/reader contract.
SCHEMA_VERSION = 2


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
    for prefix, role in (("Check-in:", "in"), ("Check-out:", "out")):
        if summary.startswith(prefix):
            m = re.search(r"trip/show/id/(\d+)", description)
            if not m:
                return None, None
            hotel = summary[len(prefix) :].strip()
            return role, (m.group(1), hotel)
    return None, None


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
            datetime.strptime(s.rstrip("Z"), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc),
            True,
        )
    except ValueError:
        pass
    try:
        return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc), False
    except ValueError:
        return None, False


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

    now = datetime.now(timezone.utc)

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
            continue

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
                "summary": _get_field(component, "SUMMARY"),
                "start": start,
                "start_timed": start_timed,
                "end": end,
                "end_timed": end_timed,
                "location": _get_field(component, "LOCATION"),
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

        events.append(
            {
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
        )

    events.sort(key=lambda e: e["start"])

    # Atomic write per `coding-policy: stateful-artifacts`: stage a
    # same-dir `.tmp` sibling, validate it, then `os.replace` it into
    # place. A failed or partial refresh never truncates or freshens the
    # live travel-schedule.json — the mtime-preserving failure contract
    # the sibling state-schema.md documents. Mirrors build-travel-db.py.
    tmp_path = OUTPUT_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(events, f, indent=2)

    # Validate the staged file before it goes live.
    with open(tmp_path) as f:
        verified = json.load(f)
    assert isinstance(verified, list), "travel-schedule.json is not a JSON array"
    assert all("summary" in e and "start" in e and "end" in e and "type" in e for e in verified), (
        "travel-schedule.json contains events missing required fields"
    )

    os.replace(tmp_path, OUTPUT_PATH)

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
