"""Baseline tests for nightly-travel-sync/scripts/refresh-travel-schedule.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Reads the ICS feed URL from `URL_PATH` (one line, stripped); empty file
    → RuntimeError with the path in the message
  - Fetches via `urllib.request.urlopen` with one retry on transient
    failure; both attempts failing → RuntimeError including the last
    error
  - Unfolds RFC-5545 CRLF + whitespace continuations before parsing
  - One event per `BEGIN:VEVENT` segment; events whose `DTEND` is
    before `now` are filtered
  - Event type derives from UID + DESCRIPTION:
      * UID without `item-` → `'Trip'`
      * UID with `item-` and DESCRIPTION matching `[Type]` → that Type
      * UID with `item-` and no `[Type]` marker → `'Unknown'`
  - Output JSON is a list sorted ascending by `start`, each entry
    carrying `summary` / `start` / `end` / `location` / `type` / `uid`
  - On success, the script also re-reads the file and asserts
    `isinstance(verified, list)` plus the four required keys per entry

Tests freeze `module.datetime` (now() returning a fixed UTC instant) so
the `DTEND < now` filter is deterministic and patch
`urllib.request.urlopen` to feed canned ICS bodies.
"""

import json
from datetime import datetime, timezone

import pytest

_FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _make_frozen_datetime(real_datetime):
    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return _FROZEN_NOW.replace(tzinfo=None)
            return _FROZEN_NOW.astimezone(tz)

    return FrozenDateTime


class _FakeResponse:
    def __init__(self, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, *responses):
    """Patch `urllib.request.urlopen` to return successive responses
    per call. Each item is either a body (bytes/str — wrapped in
    `_FakeResponse`) or an `Exception` (raised). Used to drive the
    1-retry behavior."""
    queue = list(responses)

    def _fake(url, timeout=None):
        if not queue:
            raise AssertionError("urlopen called more times than mocked")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)

    monkeypatch.setattr("urllib.request.urlopen", _fake)


def _ics(*vevents, prelude="BEGIN:VCALENDAR\r\nVERSION:2.0\r\n", trailer="END:VCALENDAR\r\n"):
    """Compose a minimal ICS body from VEVENT blocks. Each `vevent` is
    an iterable of (key, value) tuples that becomes one
    BEGIN:VEVENT / END:VEVENT segment with the given fields."""
    parts = [prelude]
    for fields in vevents:
        parts.append("BEGIN:VEVENT\r\n")
        for k, v in fields:
            parts.append(f"{k}:{v}\r\n")
        parts.append("END:VEVENT\r\n")
    parts.append(trailer)
    return "".join(parts)


def _run(module, monkeypatch, capsys):
    """Invoke main() with frozen datetime and captured stdout."""
    monkeypatch.setattr("sys.argv", ["refresh-travel-schedule.py"])
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    code = 0
    try:
        result = module.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# URL-file guard
# ---------------------------------------------------------------------------


def test_empty_url_file_raises_runtime_error(refresh_travel_schedule, monkeypatch, capsys):
    """Empty url file → RuntimeError carrying the path so an operator
    can fix it."""
    module, url_path, _ = refresh_travel_schedule
    url_path.write_text("")

    with pytest.raises(RuntimeError) as excinfo:
        _run(module, monkeypatch, capsys)
    assert str(url_path) in str(excinfo.value)


# ---------------------------------------------------------------------------
# Fetch retry semantics
# ---------------------------------------------------------------------------


def test_retry_after_first_failure_succeeds(refresh_travel_schedule, monkeypatch, capsys):
    """First urlopen raises, second returns ICS → no exception, output
    file written. The 5-second sleep between attempts is patched to
    return immediately to keep the test fast."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260601"),
            ("DTEND", "20260605"),
            ("SUMMARY", "Madrid trip"),
            ("LOCATION", "Madrid, ES"),
            ("UID", "trip-100@tripit.com"),
            ("DESCRIPTION", "Solo recharge"),
        ],
    )
    _patch_urlopen(monkeypatch, ConnectionError("transient"), body)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)

    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    assert output_path.exists()
    events = json.loads(output_path.read_text())
    assert events[0]["summary"] == "Madrid trip"


def test_both_attempts_fail_raises(refresh_travel_schedule, monkeypatch, capsys):
    """Both attempts fail → RuntimeError mentions the last error so a
    human reading the run log can act on it."""
    module, url_path, _ = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    _patch_urlopen(monkeypatch, ConnectionError("first boom"), TimeoutError("second boom"))
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)

    with pytest.raises(RuntimeError) as excinfo:
        _run(module, monkeypatch, capsys)
    assert "second boom" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Parse semantics
# ---------------------------------------------------------------------------


def test_past_dtend_events_filtered(refresh_travel_schedule, monkeypatch, capsys):
    """Events with DTEND before frozen-now are dropped from the output
    file (frozen-now: 2026-04-30 UTC)."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260101"),
            ("DTEND", "20260105"),
            ("SUMMARY", "Past trip"),
            ("UID", "trip-1@tripit.com"),
            ("DESCRIPTION", ""),
        ],
        [
            ("DTSTART", "20260601"),
            ("DTEND", "20260605"),
            ("SUMMARY", "Future trip"),
            ("UID", "trip-2@tripit.com"),
            ("DESCRIPTION", ""),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    summaries = [e["summary"] for e in events]
    assert summaries == ["Future trip"]


def test_today_timed_event_preserved(refresh_travel_schedule, monkeypatch, capsys):
    """Timed VEVENT on frozen-now's date (`20260430T180000Z`, six hours
    after frozen-now's 12:00 UTC) is NOT filtered as past — regression
    of #229 fix path. Pre-fix `parse_dt` stripped the time component
    and mapped DTEND to midnight, so today's flights vanished once UTC
    midnight had rolled over."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260430T093000Z"),
            ("DTEND", "20260430T180000Z"),
            ("SUMMARY", "KL1218 ARN-AMS"),
            ("UID", "item-200@tripit.com"),
            ("DESCRIPTION", "[Flight] ARN to AMS"),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    assert [e["summary"] for e in events] == ["KL1218 ARN-AMS"]


def test_timed_event_emits_iso_datetime(refresh_travel_schedule, monkeypatch, capsys):
    """Timed VEVENTs (flights, lodging check-ins, rentals — `YYYYMMDDTHHMMSSZ`)
    surface their time-of-day in the emitted `start`/`end` per
    `nanoclaw-admin#289`. Pre-fix the emit step stripped the time so
    downstream consumers couldn't answer "what time does today's flight
    depart?" without a Composio round-trip even though the source ICS
    carried `DTSTART:20260522T070000Z`."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260522T070000Z"),
            ("DTEND", "20260522T140000Z"),
            ("SUMMARY", "DL23 MUC to DTW"),
            ("UID", "item-300@tripit.com"),
            ("DESCRIPTION", "[Flight] MUC to DTW"),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    assert events[0]["start"] == "2026-05-22T07:00:00Z"
    assert events[0]["end"] == "2026-05-22T14:00:00Z"
    # v2: the writer persists DESCRIPTION verbatim — the `[Flight] <DEP> to <ARR>`
    # route line drive-engine's TripIt-union parser reads (#156 R2). Locks the
    # writer↔parser contract so a real Flight row is never silently unparseable.
    assert events[0]["description"] == "[Flight] MUC to DTW"


def test_date_only_event_stays_date_only(refresh_travel_schedule, monkeypatch, capsys):
    """Date-only VEVENTs (trip-level wrappers — `DTSTART;VALUE=DATE:YYYYMMDD`)
    keep emitting `YYYY-MM-DD` after `nanoclaw-admin#289` — emitting
    midnight on those would invent a precision the source feed doesn't
    have, and downstream day-keyed consumers would have to special-case
    the synthetic time."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260601"),
            ("DTEND", "20260605"),
            ("SUMMARY", "Madrid trip"),
            ("UID", "trip-1@tripit.com"),
            ("DESCRIPTION", ""),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    assert events[0]["start"] == "2026-06-01"
    assert events[0]["end"] == "2026-06-05"


def test_date_only_dtstart_still_parsed(refresh_travel_schedule, monkeypatch, capsys):
    """Date-only DTSTART (`DTSTART;VALUE=DATE:20260601` → field value
    `20260601`) still parses via the date-only fallback branch — the
    trip-level VEVENT shape must keep working after the
    timed-form-first reorder."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260601"),
            ("DTEND", "20260605"),
            ("SUMMARY", "Madrid trip"),
            ("UID", "trip-1@tripit.com"),
            ("DESCRIPTION", ""),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    assert events[0]["summary"] == "Madrid trip"


def test_event_type_classification(refresh_travel_schedule, monkeypatch, capsys):
    """Three branches:
    - UID without `item-` → 'Trip'
    - UID with `item-` + DESCRIPTION `[Flight]` → 'Flight'
    - UID with `item-` + no [Type] marker → 'Unknown'
    """
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260601"),
            ("DTEND", "20260605"),
            ("SUMMARY", "Trip-shape"),
            ("UID", "trip-100@tripit.com"),
            ("DESCRIPTION", ""),
        ],
        [
            ("DTSTART", "20260602"),
            ("DTEND", "20260603"),
            ("SUMMARY", "Flight ATL→MAD"),
            ("UID", "item-101@tripit.com"),
            ("DESCRIPTION", "[Flight] ATL to MAD"),
        ],
        [
            ("DTSTART", "20260604"),
            ("DTEND", "20260605"),
            ("SUMMARY", "Marker-less item"),
            ("UID", "item-102@tripit.com"),
            ("DESCRIPTION", "no marker"),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    type_by_summary = {e["summary"]: e["type"] for e in events}
    assert type_by_summary == {
        "Trip-shape": "Trip",
        "Flight ATL→MAD": "Flight",
        "Marker-less item": "Unknown",
    }


def test_events_sorted_by_start(refresh_travel_schedule, monkeypatch, capsys):
    """Output is sorted ascending by `start` (YYYY-MM-DD strings sort
    lexicographically and chronologically)."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260815"),
            ("DTEND", "20260820"),
            ("SUMMARY", "Late"),
            ("UID", "trip-3@tripit.com"),
            ("DESCRIPTION", ""),
        ],
        [
            ("DTSTART", "20260601"),
            ("DTEND", "20260603"),
            ("SUMMARY", "Early"),
            ("UID", "trip-4@tripit.com"),
            ("DESCRIPTION", ""),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    assert [e["summary"] for e in events] == ["Early", "Late"]


def test_ics_line_continuation_unfolded(refresh_travel_schedule, monkeypatch, capsys):
    """RFC-5545 line continuation (CRLF + leading whitespace) is
    unfolded before the field-extraction regex runs, so a wrapped
    DESCRIPTION still produces the right `[Type]`."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    # DESCRIPTION wrapped onto two lines per RFC-5545: line continues
    # if the next line begins with a space or tab.
    body = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "DTSTART:20260601\r\n"
        "DTEND:20260603\r\n"
        "SUMMARY:Wrapped flight\r\n"
        "UID:item-200@tripit.com\r\n"
        "DESCRIPTION:[Flig\r\n ht] wrapped at the bracket\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    assert events[0]["type"] == "Flight"


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


def test_output_carries_required_keys(refresh_travel_schedule, monkeypatch, capsys):
    """Every emitted entry has summary / start / end / type — the
    invariants the script's own `assert all(...)` post-write check
    relies on."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260601"),
            ("DTEND", "20260605"),
            ("SUMMARY", "Madrid"),
            ("LOCATION", "Madrid, ES"),
            ("UID", "trip-1@tripit.com"),
            ("DESCRIPTION", ""),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    assert isinstance(events, list)
    # Every record carries schema_version per `coding-policy:
    # stateful-artifacts` (see state-schema.md) — the artifact is a bare
    # array, so the version lives per-record.
    required = {"schema_version", "summary", "start", "end", "type"}
    for ev in events:
        assert required <= ev.keys(), ev
        assert ev["schema_version"] == module.SCHEMA_VERSION


def test_stdout_breakdown_summary(refresh_travel_schedule, monkeypatch, capsys):
    """The script's stdout is a JSON run summary — event count + a
    Counter-based type breakdown — the operator-facing signal in nightly
    logs (per `coding-policy: script-delegation`, JSON-producing)."""
    module, url_path, _ = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260601"),
            ("DTEND", "20260605"),
            ("SUMMARY", "Trip A"),
            ("UID", "trip-1@tripit.com"),
            ("DESCRIPTION", ""),
        ],
        [
            ("DTSTART", "20260602"),
            ("DTEND", "20260603"),
            ("SUMMARY", "Flight A"),
            ("UID", "item-1@tripit.com"),
            ("DESCRIPTION", "[Flight] X to Y"),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert payload["events_written"] == 2
    assert payload["type_breakdown"] == {"Trip": 1, "Flight": 1}


# ---------------------------------------------------------------------------
# Lodging check-in / check-out pairing (issue #41)
# ---------------------------------------------------------------------------


def test_lodging_checkin_retained_while_stay_live(refresh_travel_schedule, monkeypatch, capsys):
    """A Lodging `Check-in:` VEVENT whose own DTEND is already past
    (check-in instant + 1h) is RETAINED when its matching `Check-out:`
    (same trip-ID + hotel) is still in the future — frozen-now is
    2026-04-30 12:00 UTC, the stay runs 04-29 → 05-02. Pre-fix the
    per-event `end < now` filter dropped the check-in, orphaning the
    check-out so check-travel-bookings.py reported a false 'no hotel'
    gap (issue #41)."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    trip_url = "https://www.tripit.com/trip/show/id/377069320"
    body = _ics(
        [
            ("DTSTART", "20260429T140000Z"),
            ("DTEND", "20260429T150000Z"),
            ("SUMMARY", "Check-in: Airbnb - Bruno"),
            ("LOCATION", "Estoril, PT"),
            ("UID", "item-checkin@tripit.com"),
            ("DESCRIPTION", f"[Lodging] Airbnb - Bruno {trip_url}"),
        ],
        [
            ("DTSTART", "20260502T100000Z"),
            ("DTEND", "20260502T110000Z"),
            ("SUMMARY", "Check-out: Airbnb - Bruno"),
            ("LOCATION", "Estoril, PT"),
            ("UID", "item-checkout@tripit.com"),
            ("DESCRIPTION", f"[Lodging] Airbnb - Bruno {trip_url}"),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    summaries = [e["summary"] for e in events]
    assert "Check-in: Airbnb - Bruno" in summaries
    assert "Check-out: Airbnb - Bruno" in summaries


def test_lodging_fully_past_stay_dropped(refresh_travel_schedule, monkeypatch, capsys):
    """Once the stay is over (both check-in AND check-out DTEND before
    frozen-now), the check-in is no longer rescued — there is no live
    check-out, so both events drop as ordinary past events."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    trip_url = "https://www.tripit.com/trip/show/id/100"
    body = _ics(
        [
            ("DTSTART", "20260410T140000Z"),
            ("DTEND", "20260410T150000Z"),
            ("SUMMARY", "Check-in: Old Hotel"),
            ("UID", "item-oldin@tripit.com"),
            ("DESCRIPTION", f"[Lodging] Old Hotel {trip_url}"),
        ],
        [
            ("DTSTART", "20260412T100000Z"),
            ("DTEND", "20260412T110000Z"),
            ("SUMMARY", "Check-out: Old Hotel"),
            ("UID", "item-oldout@tripit.com"),
            ("DESCRIPTION", f"[Lodging] Old Hotel {trip_url}"),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    assert events == []


def test_lodging_checkin_not_rescued_across_trips(refresh_travel_schedule, monkeypatch, capsys):
    """The retain rule keys on trip-ID + hotel, not hotel alone. A past
    stay at 'Marriott' (trip 100, fully past) must NOT be rescued by a
    live future check-out at the same-named 'Marriott' in a different
    trip (trip 200). The past trip's orphaned check-in stays dropped."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260410T140000Z"),
            ("DTEND", "20260410T150000Z"),
            ("SUMMARY", "Check-in: Marriott"),
            ("UID", "item-trip100in@tripit.com"),
            ("DESCRIPTION", "[Lodging] Marriott https://www.tripit.com/trip/show/id/100"),
        ],
        [
            ("DTSTART", "20260502T100000Z"),
            ("DTEND", "20260502T110000Z"),
            ("SUMMARY", "Check-out: Marriott"),
            ("UID", "item-trip200out@tripit.com"),
            ("DESCRIPTION", "[Lodging] Marriott https://www.tripit.com/trip/show/id/200"),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    uids = [e["uid"] for e in events]
    assert "item-trip100in@tripit.com" not in uids
    assert "item-trip200out@tripit.com" in uids


def test_lodging_pairing_requires_trip_id(refresh_travel_schedule, monkeypatch, capsys):
    """Pairing keys on trip-ID + hotel, and the trip-ID is mandatory. A
    `Check-in:`/`Check-out:` pair whose DESCRIPTION carries no
    `trip/show/id/<n>` URL is NOT paired — so a past check-in is not
    rescued even though a future check-out at the same hotel exists.
    This keeps a feed that drops/changes the trip-URL format from
    silently rescuing unrelated past check-ins across trips (a
    false-negative); the check-in falls back to the plain past-event
    filter and the visible #41 gap alert is the worst case."""
    module, url_path, output_path = refresh_travel_schedule
    url_path.write_text("https://tripit.example.test/feed.ics\n")
    body = _ics(
        [
            ("DTSTART", "20260429T140000Z"),
            ("DTEND", "20260429T150000Z"),
            ("SUMMARY", "Check-in: Untagged Inn"),
            ("UID", "item-untaggedin@tripit.com"),
            ("DESCRIPTION", "[Lodging] Untagged Inn"),
        ],
        [
            ("DTSTART", "20260502T100000Z"),
            ("DTEND", "20260502T110000Z"),
            ("SUMMARY", "Check-out: Untagged Inn"),
            ("UID", "item-untaggedout@tripit.com"),
            ("DESCRIPTION", "[Lodging] Untagged Inn"),
        ],
    )
    _patch_urlopen(monkeypatch, body)

    _run(module, monkeypatch, capsys)
    events = json.loads(output_path.read_text())
    summaries = [e["summary"] for e in events]
    assert "Check-in: Untagged Inn" not in summaries  # past, unrescued
    assert "Check-out: Untagged Inn" in summaries  # future, kept on its own
