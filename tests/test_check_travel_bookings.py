"""Baseline tests for skills/check-travel-bookings/scripts/check-travel-bookings.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - DB-only flow: `travel-db.json` is the sole input. A missing,
    unreadable, or structurally invalid file is a hard error
    (`{"error": "..."}` + exit 1) — no live-ICS fallback. The
    alerting surface is `nightly-travel-sync` Step 4's failure
    branch (notify + daily cron re-run), not the Step 3 probe
    (which only watches `travel-schedule.json`)
  - Past trips (`trip_end < today`) are filtered before classification
  - `classify_trip` produces:
      * `is_empty: True` when the trip has zero items
      * `has_transport` True if any item is `Flight` or `Rail`
      * `has_lodging` True if any item is `Lodging`
      * `uncovered_nights` lists ISO date strings for each
        non-travel-night without lodging coverage AND with at least
        one future transport date — the "no future transport = home"
        guard prevents tail-end home-nights from being flagged
      * the night scan is floored at the injected `today` — elapsed
        nights of a trip already underway are never flagged (#120)
  - `build_lodging_ranges` pairs `Check-in:` / `Check-out:` events by
    hotel name; an orphan check-in defaults to a 1-day stay
    (the trip slug itself is `travel-core`'s `trip_key`, covered by
    `tests/test_trip_key.py`)
  - Issue derivation prioritizes empty > transport-without-lodging >
    transport-with-uncovered-nights; trips with all checks passing
    increment `complete_trips` and emit nothing
  - Snooze gate: a `snooze_until >= today` entry in
    `travel-booking-state.json` keyed by trip slug suppresses the
    gap and counts the trip as complete; expired snoozes are ignored
  - Output: `{gaps[], checked_at, total_trips, complete_trips}`
    indented JSON to stdout

Tests freeze `module.date` (today) and `module.datetime` (now) so
`checked_at` and tail-night logic are deterministic.
"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

_FROZEN_TODAY = date(2026, 4, 30)
# Verdict expiries either side of the frozen clock, so "expired" is a fixture
# fact rather than a function of when the suite runs.
_ACTIVE_EXPIRY = "2026-05-19T00:00:00+00:00"
_LAPSED_EXPIRY = "2026-04-29T00:00:00+00:00"
_FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _make_frozen_date(real_date):
    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return _FROZEN_TODAY

    return FrozenDate


def _make_frozen_datetime(real_datetime):
    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return _FROZEN_NOW.replace(tzinfo=None)
            return _FROZEN_NOW.astimezone(tz)

    return FrozenDateTime


def _db_payload(trips):
    """Build a `travel-db.json` payload. `generated_at` is the frozen
    now — the field is included for shape fidelity with what
    `build-travel-db.py` writes, even though the reader no longer
    inspects it."""
    return {
        "generated_at": _FROZEN_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trips": trips,
    }


def _trip_record(*, summary, start, end, days):
    """Compose a single trip entry for travel-db.json. `days` is a
    dict {ISO-date: [item-dict, ...]} matching the build-travel-db
    output shape."""
    return {
        "summary": summary,
        "start": start.isoformat() if isinstance(start, date) else start,
        "end": end.isoformat() if isinstance(end, date) else end,
        "days": days,
    }


def _item(*, type, summary, start, end=None, uid="item-1@tripit"):
    if end is None:
        end = start
    return {
        "type": type,
        "summary": summary,
        "start": start.isoformat() if isinstance(start, date) else start,
        "end": end.isoformat() if isinstance(end, date) else end,
        "uid": uid,
    }


def _run(module, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["check-travel-bookings.py"])
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    code = 0
    try:
        result = module.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_calendar_only_flight_gap_uses_the_existing_report(
    check_travel_bookings,
    monkeypatch,
    capsys,
    tmp_path,
):
    module, db, _ = check_travel_bookings
    start = _FROZEN_NOW + timedelta(days=1)
    db.write_text(json.dumps({"schema_version": 3, "trips": {}}))
    (tmp_path / "drive-decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "trips": {
                    "meeting:pickup": {
                        "verdict": "fly",
                        "decided_by": "operator",
                        "expires": (start + timedelta(days=2)).isoformat(),
                        "meeting": {
                            "summary": "Pickup",
                            "start": start.isoformat(),
                            "end": (start + timedelta(hours=1)).isoformat(),
                            "destination": "ATL",
                            "gap_reason": None,
                        },
                    }
                },
            }
        )
    )
    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    [gap] = json.loads(out)["gaps"]
    assert gap["trip"] == "Pickup" and gap["slug"] == "meeting:pickup"
    assert "рейс или поезд" in gap["issue"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_lodging_ranges_pairs_by_hotel(check_travel_bookings):
    """`Check-in: Hotel X` + `Check-out: Hotel X` → (in, out) range."""
    module, *_ = check_travel_bookings
    items = [
        {
            "summary": "Check-in: Hotel Sol",
            "dtstart": _FROZEN_TODAY + timedelta(days=10),
        },
        {
            "summary": "Check-out: Hotel Sol",
            "dtstart": _FROZEN_TODAY + timedelta(days=13),
        },
    ]
    ranges = module.build_lodging_ranges(items)
    assert ranges == [(_FROZEN_TODAY + timedelta(days=10), _FROZEN_TODAY + timedelta(days=13))]


def test_build_lodging_ranges_orphan_checkin_defaults_one_day(check_travel_bookings):
    """Orphaned `Check-in:` with no matching `Check-out:` → 1-day
    default range so the trip's lodging coverage isn't silently
    erased."""
    module, *_ = check_travel_bookings
    items = [
        {
            "summary": "Check-in: Hotel Sol",
            "dtstart": _FROZEN_TODAY + timedelta(days=10),
        },
    ]
    ranges = module.build_lodging_ranges(items)
    assert ranges == [(_FROZEN_TODAY + timedelta(days=10), _FROZEN_TODAY + timedelta(days=11))]


def test_build_lodging_ranges_multiple_stays_same_hotel(check_travel_bookings):
    """Two separate stays at the same hotel (bookending a multi-city
    trip) produce two distinct ranges, paired chronologically — not one
    collapsed range that would under-report coverage."""
    module, *_ = check_travel_bookings
    items = [
        {"summary": "Check-in: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=10)},
        {"summary": "Check-out: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=12)},
        {"summary": "Check-in: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=20)},
        {"summary": "Check-out: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=22)},
    ]
    ranges = module.build_lodging_ranges(items)
    assert ranges == [
        (_FROZEN_TODAY + timedelta(days=10), _FROZEN_TODAY + timedelta(days=12)),
        (_FROZEN_TODAY + timedelta(days=20), _FROZEN_TODAY + timedelta(days=22)),
    ]


def test_build_lodging_ranges_same_hotel_extra_checkin_defaults_one_day(check_travel_bookings):
    """Same hotel with two check-ins but only one check-out: the earlier
    stay pairs with the check-out, the unpaired second check-in falls
    back to a 1-day range rather than being dropped."""
    module, *_ = check_travel_bookings
    items = [
        {"summary": "Check-in: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=10)},
        {"summary": "Check-out: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=12)},
        {"summary": "Check-in: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=20)},
    ]
    ranges = module.build_lodging_ranges(items)
    assert ranges == [
        (_FROZEN_TODAY + timedelta(days=10), _FROZEN_TODAY + timedelta(days=12)),
        (_FROZEN_TODAY + timedelta(days=20), _FROZEN_TODAY + timedelta(days=21)),
    ]


def test_build_lodging_ranges_stray_earlier_checkout_not_consumed(check_travel_bookings):
    """A stray check-out earlier than the check-in must not steal the
    slot of the valid later check-out: greedy pairing skips it and the
    check-in matches the day-12 check-out, not a 1-day fallback."""
    module, *_ = check_travel_bookings
    items = [
        {"summary": "Check-out: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=9)},
        {"summary": "Check-in: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=10)},
        {"summary": "Check-out: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=12)},
    ]
    ranges = module.build_lodging_ranges(items)
    assert ranges == [(_FROZEN_TODAY + timedelta(days=10), _FROZEN_TODAY + timedelta(days=12))]


def test_build_lodging_ranges_orphan_earlier_checkin_not_stealing_later_stay(check_travel_bookings):
    """An orphan earlier check-in (no check-out of its own) must not
    consume the later valid stay's check-out: it falls back to 1 day and
    the day-10→day-12 stay is reported intact — not an over-reported
    day-5→day-12 range that would hide uncovered nights."""
    module, *_ = check_travel_bookings
    items = [
        {"summary": "Check-in: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=5)},
        {"summary": "Check-in: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=10)},
        {"summary": "Check-out: Hotel Sol", "dtstart": _FROZEN_TODAY + timedelta(days=12)},
    ]
    ranges = module.build_lodging_ranges(items)
    assert ranges == [
        (_FROZEN_TODAY + timedelta(days=5), _FROZEN_TODAY + timedelta(days=6)),
        (_FROZEN_TODAY + timedelta(days=10), _FROZEN_TODAY + timedelta(days=12)),
    ]


# ---------------------------------------------------------------------------
# classify_trip branches
# ---------------------------------------------------------------------------


def test_classify_trip_empty(check_travel_bookings):
    module, *_ = check_travel_bookings
    out = module.classify_trip(
        items=[],
        trip_start=_FROZEN_TODAY,
        trip_end=_FROZEN_TODAY + timedelta(days=3),
        today=_FROZEN_TODAY,
    )
    assert out["is_empty"] is True
    assert out["has_transport"] is False


def test_classify_trip_empty_underway_has_no_bookable_window(check_travel_bookings):
    """#286: the trip started 6 days ago and ends tomorrow. It really is
    empty, so `is_empty` stays true — but there is nothing left to book,
    which is what the alert gates on."""
    module, *_ = check_travel_bookings
    out = module.classify_trip(
        items=[],
        trip_start=_FROZEN_TODAY - timedelta(days=6),
        trip_end=_FROZEN_TODAY + timedelta(days=1),
        today=_FROZEN_TODAY,
    )
    assert out["is_empty"] is True
    assert out["has_bookable_window"] is False


def test_classify_trip_empty_future_still_has_a_bookable_window(check_travel_bookings):
    """The signal this check exists for must survive: a FUTURE empty
    away-trip still has a window, so it still surfaces (#271)."""
    module, *_ = check_travel_bookings
    out = module.classify_trip(
        items=[],
        trip_start=_FROZEN_TODAY + timedelta(days=5),
        trip_end=_FROZEN_TODAY + timedelta(days=8),
        today=_FROZEN_TODAY,
    )
    assert out["is_empty"] is True
    assert out["has_bookable_window"] is True


def test_classify_trip_empty_starting_today_has_no_bookable_window(check_travel_bookings):
    """The boundary. A trip whose first day is today has already begun;
    `today < trip_start` is false, so the window is closed."""
    module, *_ = check_travel_bookings
    out = module.classify_trip(
        items=[],
        trip_start=_FROZEN_TODAY,
        trip_end=_FROZEN_TODAY + timedelta(days=3),
        today=_FROZEN_TODAY,
    )
    assert out["is_empty"] is True
    assert out["has_bookable_window"] is False


def test_classify_trip_transport_without_lodging(check_travel_bookings):
    """Has flight, no lodging → uncovered_nights covers every
    non-travel night BEFORE the last transport (tail-end home-nights
    are NOT flagged)."""
    module, *_ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=14)
    items = [
        {
            "item_type": "Flight",
            "summary": "Outbound",
            "dtstart": trip_start,
            "dtend": trip_start,
        },
        {
            "item_type": "Flight",
            "summary": "Return",
            "dtstart": trip_start + timedelta(days=3),
            "dtend": trip_start + timedelta(days=3),
        },
    ]
    out = module.classify_trip(items, trip_start, trip_end, today=_FROZEN_TODAY)
    assert out["has_transport"] is True
    assert out["has_lodging"] is False
    # Nights at indices 1, 2 are uncovered (between outbound day-0
    # and return day-3, exclusive of travel days).
    expected = [
        (trip_start + timedelta(days=1)).isoformat(),
        (trip_start + timedelta(days=2)).isoformat(),
    ]
    assert out["uncovered_nights"] == expected


def test_classify_trip_full_coverage(check_travel_bookings):
    """Has flight + lodging spanning every non-travel night → zero
    uncovered."""
    module, *_ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=14)
    items = [
        {
            "item_type": "Flight",
            "summary": "Outbound",
            "dtstart": trip_start,
            "dtend": trip_start,
        },
        {
            "item_type": "Lodging",
            "summary": "Check-in: Hotel",
            "dtstart": trip_start,
            "dtend": trip_start,
        },
        {
            "item_type": "Lodging",
            "summary": "Check-out: Hotel",
            "dtstart": trip_start + timedelta(days=4),
            "dtend": trip_start + timedelta(days=4),
        },
        {
            "item_type": "Flight",
            "summary": "Return",
            "dtstart": trip_start + timedelta(days=4),
            "dtend": trip_start + timedelta(days=4),
        },
    ]
    out = module.classify_trip(items, trip_start, trip_end, today=_FROZEN_TODAY)
    assert out["uncovered_nights"] == []


def test_classify_trip_tail_home_night_not_flagged(check_travel_bookings):
    """The "no future transport = traveller is home" guard: a night
    after the last transport date is NOT flagged as uncovered, even
    when no lodging covers it. Without this guard, the next trip's
    outbound flight (pulled in by overlap query) would falsely flag
    the home-tail."""
    module, *_ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=15)
    # Only one transport item, very early — the rest of the window is
    # post-transport (home).
    items = [
        {
            "item_type": "Flight",
            "summary": "Outbound",
            "dtstart": trip_start,
            "dtend": trip_start,
        },
    ]
    out = module.classify_trip(items, trip_start, trip_end, today=_FROZEN_TODAY)
    # No future transport after night 0 → no uncovered flagged.
    assert out["uncovered_nights"] == []


def test_classify_trip_same_day_round_trip_no_uncovered(check_travel_bookings):
    """A same-day round trip (out + back on one calendar day, the
    return leg's arrival slipping past UTC midnight so trip_end is the
    next day) needs no hotel: the single trip night IS a travel night,
    so uncovered_nights is empty even with zero lodging. Regression
    guard for admin#310."""
    module, *_ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = trip_start + timedelta(days=1)
    items = [
        {
            "item_type": "Flight",
            "summary": "Outbound BNA→MIA",
            "dtstart": trip_start,
            "dtend": trip_start,
        },
        {
            "item_type": "Flight",
            "summary": "Return FLL→BNA",
            "dtstart": trip_start,
            "dtend": trip_end,  # arrival slips past UTC midnight
        },
    ]
    out = module.classify_trip(items, trip_start, trip_end, today=_FROZEN_TODAY)
    assert out["has_transport"] is True
    assert out["has_lodging"] is False
    assert out["uncovered_nights"] == []


def test_classify_trip_past_nights_not_flagged(check_travel_bookings):
    """Trip already underway: nights before `today` are elapsed and
    un-bookable, so they are never flagged even without lodging
    coverage. Lodging covering today onward → zero uncovered.
    Regression guard for #120 (Scotland trip, 2026-07-07: 10 phantom
    past-night gaps buried the correctly-matched current Airbnb)."""
    module, *_ = check_travel_bookings
    trip_start = _FROZEN_TODAY - timedelta(days=11)
    trip_end = _FROZEN_TODAY + timedelta(days=6)
    items = [
        {
            "item_type": "Flight",
            "summary": "Outbound",
            "dtstart": trip_start,
            "dtend": trip_start,
        },
        {
            "item_type": "Lodging",
            "summary": "Check-in: Airbnb - Jane",
            "dtstart": _FROZEN_TODAY - timedelta(days=1),
            "dtend": _FROZEN_TODAY - timedelta(days=1),
        },
        {
            "item_type": "Lodging",
            "summary": "Check-out: Airbnb - Jane",
            "dtstart": _FROZEN_TODAY + timedelta(days=4),
            "dtend": _FROZEN_TODAY + timedelta(days=4),
        },
        {
            "item_type": "Lodging",
            "summary": "Check-in: Airport Hotel",
            "dtstart": _FROZEN_TODAY + timedelta(days=4),
            "dtend": _FROZEN_TODAY + timedelta(days=4),
        },
        {
            "item_type": "Lodging",
            "summary": "Check-out: Airport Hotel",
            "dtstart": _FROZEN_TODAY + timedelta(days=5),
            "dtend": _FROZEN_TODAY + timedelta(days=5),
        },
        {
            # Departs the day before trip_end so it lands inside the
            # [trip_start, trip_end) transport window — without a
            # future transport date the home-guard alone would hide
            # the past-night bug this test exists to catch.
            "item_type": "Flight",
            "summary": "Return",
            "dtstart": trip_end - timedelta(days=1),
            "dtend": trip_end - timedelta(days=1),
        },
    ]
    out = module.classify_trip(items, trip_start, trip_end, today=_FROZEN_TODAY)
    assert out["uncovered_nights"] == []


def test_classify_trip_future_uncovered_still_flagged_mid_trip(check_travel_bookings):
    """The today-floor only drops elapsed nights — a genuinely
    uncovered FUTURE night of an underway trip still surfaces."""
    module, *_ = check_travel_bookings
    trip_start = _FROZEN_TODAY - timedelta(days=3)
    trip_end = _FROZEN_TODAY + timedelta(days=3)
    items = [
        {
            "item_type": "Flight",
            "summary": "Outbound",
            "dtstart": trip_start,
            "dtend": trip_start,
        },
        {
            "item_type": "Flight",
            "summary": "Return",
            "dtstart": trip_end - timedelta(days=1),
            "dtend": trip_end - timedelta(days=1),
        },
    ]
    out = module.classify_trip(items, trip_start, trip_end, today=_FROZEN_TODAY)
    # Elapsed nights (trip_start..yesterday) are dropped; tonight and
    # tomorrow remain uncovered ahead of the return leg.
    expected = [
        _FROZEN_TODAY.isoformat(),
        (_FROZEN_TODAY + timedelta(days=1)).isoformat(),
    ]
    assert out["uncovered_nights"] == expected


# ---------------------------------------------------------------------------
# load_trips_from_db freshness
# ---------------------------------------------------------------------------


def test_load_trips_from_db_returns_none_on_missing(check_travel_bookings):
    """Missing DB file → return None (main() turns this into exit 1)."""
    module, db_path, _ = check_travel_bookings
    assert not db_path.exists()
    assert module.load_trips_from_db(str(db_path)) is None


def test_load_trips_from_db_returns_none_on_corrupt(check_travel_bookings):
    """Unreadable JSON → return None (main() turns this into exit 1)."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text("{not json")
    assert module.load_trips_from_db(str(db_path)) is None


def test_load_trips_from_db_returns_none_on_permission_error(check_travel_bookings, monkeypatch):
    """PermissionError (or any other OSError) on read → return None
    so main() can emit the actionable JSON error. Without the broader
    OSError catch the traceback would escape the script and the
    caller would see a non-JSON crash."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text(json.dumps({"trips": {}}))

    def _denied(*_args, **_kwargs):
        raise PermissionError("simulated chmod 000")

    monkeypatch.setattr("builtins.open", _denied)
    assert module.load_trips_from_db(str(db_path)) is None


def test_load_trips_from_db_returns_none_on_non_utf8(check_travel_bookings):
    """Non-UTF-8 bytes (e.g., half-failed build-travel-db.py writing
    garbage) raise UnicodeDecodeError; caught and treated as
    unreadable so the hard-error JSON contract holds."""
    module, db_path, _ = check_travel_bookings
    db_path.write_bytes(b"\xff\xfe\x00\x01garbage")
    assert module.load_trips_from_db(str(db_path)) is None


def test_load_trips_from_db_returns_none_on_list_root(check_travel_bookings):
    """Parseable JSON but structurally invalid (root is a list, not
    a dict) → return None. Without the isinstance guard, `.get()`
    would AttributeError and escape as a traceback."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text(json.dumps([1, 2, 3]))
    assert module.load_trips_from_db(str(db_path)) is None


def test_load_trips_from_db_returns_none_on_list_trips(check_travel_bookings):
    """Parseable dict whose `trips` is a list, not a dict →
    return None. Without the isinstance guard on `trips`, `.items()`
    would AttributeError."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text(json.dumps({"trips": [{"summary": "x"}]}))
    assert module.load_trips_from_db(str(db_path)) is None


def test_load_trips_from_db_skips_list_valued_trip(check_travel_bookings, monkeypatch, capsys):
    """Per-trip shape error: trip value is a list, not a dict. The
    loop skips that one trip and parses the rest — never crashes
    with TypeError. Skip writes a stderr diagnostic naming the slug
    so the malformation isn't silent."""
    module, db_path, _ = check_travel_bookings
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    good_start = _FROZEN_TODAY + timedelta(days=10)
    good_end = _FROZEN_TODAY + timedelta(days=12)
    payload = {
        "trips": {
            "bad": [],  # list, not dict — would TypeError on t['start']
            "good": _trip_record(
                summary="Madrid",
                start=good_start,
                end=good_end,
                days={},
            ),
        },
    }
    db_path.write_text(json.dumps(payload))
    trips = module.load_trips_from_db(str(db_path))
    assert trips is not None
    summaries = [t["summary"] for t in trips]
    assert summaries == ["Madrid"]
    err = capsys.readouterr().err
    assert "skipped malformed trip" in err
    assert "slug='bad'" in err


def test_load_trips_from_db_skips_trip_missing_summary(check_travel_bookings, monkeypatch):
    """Per-trip shape error: trip dict missing `summary`. Skipped
    (with stderr diagnostic) rather than escaping as KeyError. This
    test asserts the survival of the good trip; the diagnostic-text
    contract is asserted in `test_load_trips_from_db_skips_list_valued_trip`."""
    module, db_path, _ = check_travel_bookings
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    good_start = _FROZEN_TODAY + timedelta(days=10)
    good_end = _FROZEN_TODAY + timedelta(days=12)
    payload = {
        "trips": {
            "no-summary": {
                "start": good_start.isoformat(),
                "end": good_end.isoformat(),
                "days": {},
            },
            "good": _trip_record(
                summary="Madrid",
                start=good_start,
                end=good_end,
                days={},
            ),
        },
    }
    db_path.write_text(json.dumps(payload))
    trips = module.load_trips_from_db(str(db_path))
    assert trips is not None
    summaries = [t["summary"] for t in trips]
    assert summaries == ["Madrid"]


def test_load_trips_from_db_skips_null_day_events(check_travel_bookings, monkeypatch, capsys):
    """`days[<date>] = null` (or any non-iterable scalar) → that day
    is skipped, the rest of the trip parses, and a stderr diagnostic
    names the slug. Without the iter() guard, `for ev in None`
    would raise TypeError and escape the loop."""
    module, db_path, _ = check_travel_bookings
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    good_start = _FROZEN_TODAY + timedelta(days=10)
    good_end = _FROZEN_TODAY + timedelta(days=12)
    payload = {
        "trips": {
            "madrid": {
                "summary": "Madrid",
                "start": good_start.isoformat(),
                "end": good_end.isoformat(),
                "days": {
                    good_start.isoformat(): None,
                    (good_start + timedelta(days=1)).isoformat(): [
                        {
                            "type": "Flight",
                            "summary": "Outbound",
                            "start": (good_start + timedelta(days=1)).isoformat(),
                            "end": (good_start + timedelta(days=1)).isoformat(),
                        },
                    ],
                },
            },
        },
    }
    db_path.write_text(json.dumps(payload))
    trips = module.load_trips_from_db(str(db_path))
    assert trips is not None
    assert len(trips) == 1
    # The non-null day's flight survived, only the null day was skipped.
    assert any(item["item_type"] == "Flight" for item in trips[0]["items"])
    err = capsys.readouterr().err
    assert "skipped non-iterable day-events" in err
    assert "slug='madrid'" in err


def test_load_trips_from_db_skips_list_valued_days(check_travel_bookings, monkeypatch):
    """Per-trip shape error: `days` is a list, not a dict. Loop
    skips that trip (with stderr diagnostic) — would otherwise
    AttributeError on .values(). Diagnostic text is asserted in
    `test_load_trips_from_db_skips_list_valued_trip`; here we only
    assert the survival of the good trip."""
    module, db_path, _ = check_travel_bookings
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    good_start = _FROZEN_TODAY + timedelta(days=10)
    good_end = _FROZEN_TODAY + timedelta(days=12)
    payload = {
        "trips": {
            "bad-days": {
                "summary": "Bad",
                "start": good_start.isoformat(),
                "end": good_end.isoformat(),
                "days": [],  # list, not dict
            },
            "good": _trip_record(
                summary="Madrid",
                start=good_start,
                end=good_end,
                days={},
            ),
        },
    }
    db_path.write_text(json.dumps(payload))
    trips = module.load_trips_from_db(str(db_path))
    assert trips is not None
    summaries = [t["summary"] for t in trips]
    assert summaries == ["Madrid"]


def test_load_trips_from_db_ignores_generated_at_age(check_travel_bookings, monkeypatch):
    """`generated_at` is no longer inspected — even a year-old DB
    parses normally. The freshness watchdog lives in
    `nightly-travel-sync`, not here."""
    module, db_path, _ = check_travel_bookings
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    ancient = (_FROZEN_NOW - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"generated_at": ancient, "trips": {}}
    db_path.write_text(json.dumps(payload))
    assert module.load_trips_from_db(str(db_path)) == []


def test_load_trips_from_db_parses_when_fresh(check_travel_bookings, monkeypatch):
    """DB present → trips parsed with `start` / `end` as `date`
    objects."""
    module, db_path, _ = check_travel_bookings
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=12)
    payload = _db_payload(
        {
            "madrid-2026-06": _trip_record(
                summary="Madrid",
                start=trip_start,
                end=trip_end,
                days={
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="Outbound", start=trip_start),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    trips = module.load_trips_from_db(str(db_path))
    assert len(trips) == 1
    assert trips[0]["start"] == trip_start
    assert trips[0]["items"][0]["item_type"] == "Flight"


def test_load_trips_from_db_tolerates_iso_datetime_item_start(check_travel_bookings, monkeypatch):
    """Items emitted with ISO-datetime `start`/`end` (timed VEVENTs
    post-`nanoclaw-admin#289`) reduce to the calendar-date `dtstart`/
    `dtend` the classifier expects — the time component is intentionally
    discarded here because gap-classification is day-granular."""
    module, db_path, _ = check_travel_bookings
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=12)
    payload = _db_payload(
        {
            "munich-2026-05": _trip_record(
                summary="Munich",
                start=trip_start,
                end=trip_end,
                days={
                    trip_start.isoformat(): [
                        _item(
                            type="Flight",
                            summary="DL23 MUC→DTW",
                            start=f"{trip_start.isoformat()}T07:00:00Z",
                            end=f"{trip_start.isoformat()}T14:00:00Z",
                        ),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    trips = module.load_trips_from_db(str(db_path))
    assert len(trips) == 1
    assert trips[0]["items"][0]["dtstart"] == trip_start
    assert trips[0]["items"][0]["dtend"] == trip_start


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


def test_main_db_path_ships_gap(check_travel_bookings, monkeypatch, capsys):
    """DB with a transport-without-lodging trip → one gap in output."""
    module, db_path, _ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=14)
    payload = _db_payload(
        {
            "madrid-2026-06": _trip_record(
                summary="Madrid",
                start=trip_start,
                end=trip_end,
                days={
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="Outbound", start=trip_start),
                    ],
                    (trip_start + timedelta(days=3)).isoformat(): [
                        _item(
                            type="Flight",
                            summary="Return",
                            start=trip_start + timedelta(days=3),
                        ),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert "source" not in output
    assert output["total_trips"] == 1
    assert output["complete_trips"] == 0
    assert len(output["gaps"]) == 1
    gap = output["gaps"][0]
    assert gap["trip"] == "Madrid"
    # Either "рейсы есть, отеля нет" (transport-without-lodging branch
    # fires first) or "нет отеля на N ноч." (uncovered-nights branch).
    # Both share the lodging-missing root.
    assert "отел" in gap["issue"]


def test_main_complete_trip_no_gap(check_travel_bookings, monkeypatch, capsys):
    """Trip with full transport + lodging → counted as complete, no
    gap in output."""
    module, db_path, _ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=12)
    payload = _db_payload(
        {
            "madrid-2026-06": _trip_record(
                summary="Madrid",
                start=trip_start,
                end=trip_end,
                days={
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="Outbound", start=trip_start),
                        _item(type="Lodging", summary="Check-in: Hotel", start=trip_start),
                    ],
                    (trip_start + timedelta(days=2)).isoformat(): [
                        _item(
                            type="Lodging",
                            summary="Check-out: Hotel",
                            start=trip_start + timedelta(days=2),
                        ),
                        _item(
                            type="Flight",
                            summary="Return",
                            start=trip_start + timedelta(days=2),
                        ),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert output["complete_trips"] == 1
    assert output["gaps"] == []


def test_main_same_day_trip_no_false_hotel_gap(check_travel_bookings, monkeypatch, capsys):
    """A same-day round trip with zero uncovered nights must NOT
    surface 'рейсы есть, отеля нет' — there's no overnight stay to
    miss. The trip counts as complete. Regression guard for admin#310:
    the transport-without-lodging branch previously fired on
    has_transport alone, flagging same-day trips that need no hotel."""
    module, db_path, _ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = trip_start + timedelta(days=1)
    payload = _db_payload(
        {
            "agentcon-miami-2026-06": _trip_record(
                summary="Agentcon Miami",
                start=trip_start,
                end=trip_end,
                days={
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="AA487 BNA→MIA", start=trip_start),
                    ],
                    # Return leg filed under the next UTC day because its
                    # arrival slips past midnight — the same shape that
                    # tripped the false flag in production.
                    trip_end.isoformat(): [
                        _item(
                            type="Flight",
                            summary="WN1852 FLL→BNA",
                            start=trip_start,
                            end=trip_end,
                        ),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["gaps"] == []
    assert output["complete_trips"] == 1


def test_main_one_night_single_leg_no_lodging_flagged(check_travel_bookings, monkeypatch, capsys):
    """A one-night trip with a single transport leg that lands before
    trip_end and no lodging is NOT a same-day round trip — the traveller
    has arrived and stays over, so it must flag 'рейсы есть, отеля нет'.
    It shares the same-day trip's shape (trip_nights == 1, uncovered
    empty because the lone night is a travel night); the distinguisher
    is that its arrival falls before trip_end rather than reaching it."""
    module, db_path, _ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = trip_start + timedelta(days=1)
    payload = _db_payload(
        {
            "oslo-talk-2026-05": _trip_record(
                summary="Oslo Talk",
                start=trip_start,
                end=trip_end,
                days={
                    # Outbound only — no return leg in the data, so the
                    # traveller is staying over and needs a hotel.
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="SK4711 CPH→OSL", start=trip_start),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["complete_trips"] == 0
    assert len(output["gaps"]) == 1
    gap = output["gaps"][0]
    assert gap["issue"] == "рейсы есть, отеля нет"
    assert gap["uncovered_nights"] == []


def test_main_multiday_single_transport_no_lodging_flagged(
    check_travel_bookings, monkeypatch, capsys
):
    """A multi-night trip with transport but no lodging must still be
    flagged 'рейсы есть, отеля нет' even when only one transport leg is
    known — classify_trip's has_future_transport guard anchors no gap
    night, so uncovered_nights is empty, but the trip genuinely needs a
    hotel. Regression guard for the same-day narrowing: the outbound
    lands well before trip_end, so the traveller is staying — the
    predicate must not let a real multi-night gap slip through as
    complete."""
    module, db_path, _ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = trip_start + timedelta(days=5)
    payload = _db_payload(
        {
            "berlin-conf-2026-05": _trip_record(
                summary="Berlin Conf",
                start=trip_start,
                end=trip_end,
                days={
                    # Only the outbound leg is in the data; no return,
                    # no lodging. classify_trip yields uncovered == [].
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="LH401 TLV→BER", start=trip_start),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["complete_trips"] == 0
    assert len(output["gaps"]) == 1
    gap = output["gaps"][0]
    assert gap["issue"] == "рейсы есть, отеля нет"
    assert gap["uncovered_nights"] == []


def test_main_one_night_connecting_outbound_no_lodging_flagged(
    check_travel_bookings, monkeypatch, capsys
):
    """A one-night trip with two same-direction legs (a connecting
    outbound, no return) is NOT a same-day round trip even though it has
    two transport legs — both land on trip_start while trip_end is the
    next day, so the traveller arrives and stays over. Leg count alone
    would mistake it for a round trip; the arrival-before-trip_end signal
    correctly flags 'рейсы есть, отеля нет'."""
    module, db_path, _ = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = trip_start + timedelta(days=1)
    payload = _db_payload(
        {
            "singapore-summit-2026-05": _trip_record(
                summary="Singapore Summit",
                start=trip_start,
                end=trip_end,
                days={
                    # Connecting outbound: both legs depart and arrive on
                    # trip_start, no return. The traveller lands at the
                    # destination and stays the night → needs a hotel.
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="LX1 CPH→FRA", start=trip_start),
                        _item(
                            type="Flight",
                            summary="SQ25 FRA→SIN",
                            start=trip_start,
                            uid="item-2@tripit",
                        ),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["complete_trips"] == 0
    assert len(output["gaps"]) == 1
    gap = output["gaps"][0]
    assert gap["issue"] == "рейсы есть, отеля нет"
    assert gap["uncovered_nights"] == []


def test_main_past_trip_filtered(check_travel_bookings, monkeypatch, capsys):
    """`trip_end < today` → trip is skipped before classification, not
    counted as complete OR a gap."""
    module, db_path, _ = check_travel_bookings
    past_start = _FROZEN_TODAY - timedelta(days=20)
    past_end = _FROZEN_TODAY - timedelta(days=15)
    payload = _db_payload(
        {
            "past-2026-04": _trip_record(
                summary="Past",
                start=past_start,
                end=past_end,
                days={},
            ),
        }
    )
    db_path.write_text(json.dumps(payload))

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert output["total_trips"] == 1
    assert output["complete_trips"] == 0
    assert output["gaps"] == []


def test_main_snooze_active_suppresses_gap(check_travel_bookings, monkeypatch, capsys):
    """`snooze_until >= today` for the trip's slug → gap suppressed
    and trip counted as complete; expired snoozes (snooze_until <
    today) are ignored."""
    module, db_path, state_path = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=14)
    payload = _db_payload(
        {
            "madrid-2026-06": _trip_record(
                summary="Madrid",
                start=trip_start,
                end=trip_end,
                days={
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="Outbound", start=trip_start),
                    ],
                    (trip_start + timedelta(days=3)).isoformat(): [
                        _item(
                            type="Flight",
                            summary="Return",
                            start=trip_start + timedelta(days=3),
                        ),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))
    state_path.write_text(
        json.dumps(
            {"madrid-2026-06": {"snooze_until": (_FROZEN_TODAY + timedelta(days=2)).isoformat()}}
        )
    )

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert output["complete_trips"] == 1
    assert output["gaps"] == []


def test_main_snooze_expired_ignored(check_travel_bookings, monkeypatch, capsys):
    """`snooze_until < today` → snooze ignored, gap reported."""
    module, db_path, state_path = check_travel_bookings
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=14)
    payload = _db_payload(
        {
            "madrid-2026-06": _trip_record(
                summary="Madrid",
                start=trip_start,
                end=trip_end,
                days={
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="Outbound", start=trip_start),
                    ],
                    (trip_start + timedelta(days=3)).isoformat(): [
                        _item(
                            type="Flight",
                            summary="Return",
                            start=trip_start + timedelta(days=3),
                        ),
                    ],
                },
            ),
        }
    )
    db_path.write_text(json.dumps(payload))
    state_path.write_text(
        json.dumps(
            {"madrid-2026-06": {"snooze_until": (_FROZEN_TODAY - timedelta(days=1)).isoformat()}}
        )
    )

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert len(output["gaps"]) == 1


def test_main_missing_db_exits_1(check_travel_bookings, monkeypatch, capsys):
    """Missing DB → JSON `{"error": "..."}` on stdout, human-readable
    diagnostic on stderr, exit 1. The Step 4 rebuild failure branch
    in `nightly-travel-sync` is the alerting surface for DB issues,
    so this script must not paper over the gap."""
    module, db_path, _ = check_travel_bookings
    assert not db_path.exists()

    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "travel-db.json" in payload["error"]
    # stderr diagnostic per `coding-policy: file-hygiene` /
    # `script-delegation` (Self-error-handling)
    assert "check-travel-bookings:" in err
    assert "travel-db.json" in err


def test_main_corrupt_db_exits_1(check_travel_bookings, monkeypatch, capsys):
    """Unreadable DB JSON → JSON `{"error": "..."}` on stdout,
    diagnostic on stderr, exit 1."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text("{not json")

    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "travel-db.json" in payload["error"]
    assert "check-travel-bookings:" in err


def test_main_forward_schema_db_surfaces_upgrade_diagnostic(
    check_travel_bookings, monkeypatch, capsys
):
    """When the DB is rejected because its schema_version is higher
    than the reader's, the operator-facing diagnostic must name the
    detected version + the actionable upgrade path — rather than the
    generic "missing/unreadable/invalid" message that points at
    Step 4 in vain (Step 4 already wrote the file successfully; it's
    just newer than this consumer)."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text(json.dumps({"schema_version": 99, "trips": {}}))

    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 1
    payload = json.loads(out)
    assert "schema_version=99" in payload["error"]
    assert "upgrade" in payload["error"]
    assert "schema_version=99" in err


def test_main_checked_at_format(check_travel_bookings, monkeypatch, capsys):
    """`checked_at` is UTC ISO-8601 with `Z` suffix per the
    documented output shape."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text(json.dumps(_db_payload({})))

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert payload["checked_at"] == "2026-04-30T12:00:00Z"


# ---------------------------------------------------------------------------
# Schema-version gate (state-schema.md sibling — stateful-artifacts contract)
# ---------------------------------------------------------------------------


def test_load_trips_from_db_accepts_explicit_schema_v1(check_travel_bookings):
    """DB stamped with `schema_version: 1` reads normally — matches what
    `build-travel-db.py` writes."""
    module, db_path, _ = check_travel_bookings
    payload = _db_payload({})
    payload["schema_version"] = 1
    db_path.write_text(json.dumps(payload))
    trips = module.load_trips_from_db(str(db_path))
    assert trips == []


def test_load_trips_from_db_accepts_missing_schema_version_as_legacy_v1(
    check_travel_bookings,
):
    """Legacy DBs from before the schema_version field was introduced
    (e.g., the rolling pre-migration state on the NAS at deploy time)
    are treated as implicit v1 — the field was introduced AT v1, no
    prior version exists, so absence is grandfathered."""
    module, db_path, _ = check_travel_bookings
    payload = _db_payload({})
    assert "schema_version" not in payload
    db_path.write_text(json.dumps(payload))
    trips = module.load_trips_from_db(str(db_path))
    assert trips == []


def test_load_trips_from_db_returns_none_on_forward_schema_version(check_travel_bookings):
    """A DB stamped with a higher-than-current schema_version is
    forward-incompatible — return None so main() lands in the
    hard-error JSON path, surfacing operator-readable diagnostics
    instead of attempting to parse an unknown shape."""
    module, db_path, _ = check_travel_bookings
    payload = _db_payload({})
    payload["schema_version"] = 99
    db_path.write_text(json.dumps(payload))
    assert module.load_trips_from_db(str(db_path)) is None


def test_load_trips_from_db_returns_none_on_non_int_schema_version(check_travel_bookings):
    """A DB whose schema_version is not an int (string, list, bool)
    is rejected — same forward-incompatibility branch."""
    module, db_path, _ = check_travel_bookings
    for bad_value in ["1", [1], True, 1.5]:
        payload = _db_payload({})
        payload["schema_version"] = bad_value
        db_path.write_text(json.dumps(payload))
        assert module.load_trips_from_db(str(db_path)) is None, f"non-int {bad_value!r} accepted"


def _madrid_gap_payload():
    """Single Madrid trip with transport but no lodging → 'рейсы есть,
    отеля нет' gap fires unless snoozed."""
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    trip_end = _FROZEN_TODAY + timedelta(days=14)
    return _db_payload(
        {
            "madrid-2026-06": _trip_record(
                summary="Madrid",
                start=trip_start,
                end=trip_end,
                days={
                    trip_start.isoformat(): [
                        _item(type="Flight", summary="Outbound", start=trip_start),
                    ],
                    (trip_start + timedelta(days=3)).isoformat(): [
                        _item(
                            type="Flight",
                            summary="Return",
                            start=trip_start + timedelta(days=3),
                        ),
                    ],
                },
            ),
        }
    )


def test_main_snooze_with_schema_v1_suppresses_gap(check_travel_bookings, monkeypatch, capsys):
    """Snooze entry stamped with `schema_version: 1` is honored —
    matches the contract the agent is now instructed to write per
    SKILL.md Step 3."""
    module, db_path, state_path = check_travel_bookings
    db_path.write_text(json.dumps(_madrid_gap_payload()))
    state_path.write_text(
        json.dumps(
            {
                "madrid-2026-06": {
                    "schema_version": 1,
                    "snooze_until": (_FROZEN_TODAY + timedelta(days=2)).isoformat(),
                }
            }
        )
    )

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert output["gaps"] == []
    assert output["complete_trips"] == 1


def test_main_snooze_legacy_missing_schema_still_honored(
    check_travel_bookings, monkeypatch, capsys
):
    """Snooze entry without `schema_version` is legacy data (implicit
    v1) — honored to preserve existing snooze state across the
    migration deploy."""
    module, db_path, state_path = check_travel_bookings
    db_path.write_text(json.dumps(_madrid_gap_payload()))
    state_path.write_text(
        json.dumps(
            {
                "madrid-2026-06": {
                    "snooze_until": (_FROZEN_TODAY + timedelta(days=2)).isoformat(),
                }
            }
        )
    )

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert output["gaps"] == []
    assert output["complete_trips"] == 1


def test_main_snooze_with_forward_schema_ignored(check_travel_bookings, monkeypatch, capsys):
    """Snooze entry with a higher-than-current schema_version is
    forward-incompatible — ignored so the gap surfaces, preventing a
    future-shape write from silently muting alerts on the current
    reader."""
    module, db_path, state_path = check_travel_bookings
    db_path.write_text(json.dumps(_madrid_gap_payload()))
    state_path.write_text(
        json.dumps(
            {
                "madrid-2026-06": {
                    "schema_version": 99,
                    "snooze_until": (_FROZEN_TODAY + timedelta(days=2)).isoformat(),
                }
            }
        )
    )

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert len(output["gaps"]) == 1
    assert output["gaps"][0]["slug"] == "madrid-2026-06"


def test_main_snooze_state_non_dict_root_treated_as_empty(
    check_travel_bookings, monkeypatch, capsys
):
    """Valid JSON in `travel-booking-state.json` whose root is a list
    (or any non-object) crashes `.get(...)` if not guarded. Per the
    advisory-snooze contract, any non-dict root means \"no snoozes
    active\" — the gap surfaces and the script doesn't blow up."""
    module, db_path, state_path = check_travel_bookings
    db_path.write_text(json.dumps(_madrid_gap_payload()))
    state_path.write_text(json.dumps(["not", "a", "dict"]))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert len(output["gaps"]) == 1


def test_main_snooze_non_dict_entry_ignored(check_travel_bookings, monkeypatch, capsys):
    """Snooze entry that's not a dict (corrupt write, manual edit
    error) — ignored without crashing. The gap surfaces so the
    operator sees the underlying booking issue."""
    module, db_path, state_path = check_travel_bookings
    db_path.write_text(json.dumps(_madrid_gap_payload()))
    state_path.write_text(json.dumps({"madrid-2026-06": "snoozed"}))

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert len(output["gaps"]) == 1


# ---------------------------------------------------------------------------
# Missing-flight gap on a drive-or-fly "fly" verdict (#231)
# ---------------------------------------------------------------------------


_TRIP_SLUG = "tn-tigers-2026-05"


def _lodging_only_db():
    """A trip with a hotel and nothing to get there on."""
    return _db_payload(
        {
            _TRIP_SLUG: _trip_record(
                summary="TN Tigers 2026",
                start=date(2026, 5, 15),
                end=date(2026, 5, 17),
                days={
                    "2026-05-15": [
                        _item(
                            type="Lodging",
                            summary="Check-in: Fairfield Inn",
                            start=date(2026, 5, 15),
                        )
                    ],
                    "2026-05-17": [
                        _item(
                            type="Lodging",
                            summary="Check-out: Fairfield Inn",
                            start=date(2026, 5, 17),
                            uid="item-2@tripit",
                        )
                    ],
                },
            )
        }
    )


def _write_verdict(tmp_path, verdict, *, slug=_TRIP_SLUG, version=1, expires=_ACTIVE_EXPIRY):
    (tmp_path / "drive-decisions.json").write_text(
        json.dumps(
            {
                "schema_version": version,
                "trips": {
                    slug: {
                        "verdict": verdict,
                        "decided_by": "operator",
                        "drive_seconds": 30000,
                        "asked_at": None,
                        "expires": expires,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_a_fly_verdict_turns_a_hotel_only_trip_into_a_gap(
    check_travel_bookings, monkeypatch, capsys, tmp_path
):
    """The mirror of "рейсы есть, отеля нет" — hotel booked, no way there."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    _write_verdict(tmp_path, "fly")

    code, out, _err = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert code == 0
    assert [g["issue"] for g in payload["gaps"]] == ["отель есть, рейса нет"]


def test_a_drive_verdict_is_not_a_gap(check_travel_bookings, monkeypatch, capsys, tmp_path):
    """The engine is planning the drive — no flight is expected."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    _write_verdict(tmp_path, "drive")

    _code, out, _err = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert payload["gaps"] == []
    assert payload["complete_trips"] == 1


def test_an_unanswered_verdict_is_surfaced_daily_as_a_question(
    check_travel_bookings, monkeypatch, capsys, tmp_path
):
    """The engine asks once and never re-asks, so a missed notice left the trip
    with no drive legs and no alert at all (#240). This check runs daily, which
    is the cadence a nudge wants — so the open question rides the same gap line,
    worded as a question and naming the reply words."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    _write_verdict(tmp_path, "unknown")

    _code, out, _err = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert [g["issue"] for g in payload["gaps"]] == [module.TRANSPORT_GAP_ASK_ISSUE]
    assert "drive" in module.TRANSPORT_GAP_ASK_ISSUE
    assert "fly" in module.TRANSPORT_GAP_ASK_ISSUE
    assert payload["complete_trips"] == 0


@pytest.mark.parametrize("verdict", ["fly", "unknown"])
def test_an_expired_verdict_raises_no_gap(
    check_travel_bookings, monkeypatch, capsys, tmp_path, verdict
):
    """The record is residue once its trip is over. Leaning on the owner's prune
    would let a finished trip keep nagging whenever that sweep has not run."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    _write_verdict(tmp_path, verdict, expires=_LAPSED_EXPIRY)

    _code, out, _err = _run(module, monkeypatch, capsys)
    assert json.loads(out)["gaps"] == []


@pytest.mark.parametrize("expires", [None, "", "not-a-date", "2026-05-19T00:00:00", 17])
def test_a_verdict_without_a_usable_expiry_raises_no_gap(
    check_travel_bookings, monkeypatch, capsys, tmp_path, expires
):
    """`expires` is required by the writer's contract, and a naive one cannot be
    compared. Absent or unusable is residue, not a live verdict."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    _write_verdict(tmp_path, "unknown", expires=expires)

    _code, out, _err = _run(module, monkeypatch, capsys)
    assert json.loads(out)["gaps"] == []


def test_the_unanswered_question_reads_differently_from_a_settled_gap(
    check_travel_bookings,
):
    """A settled `fly` gap is a booking to make; an open question is a question.
    One shared wording would tell the operator to book a flight they may not
    want, and the daily nudge would read as a stuck alert."""
    module, _db_path, _state = check_travel_bookings
    assert module.TRANSPORT_GAP_ISSUE != module.TRANSPORT_GAP_ASK_ISSUE
    assert module.TRANSPORT_GAP_ASK_ISSUE.startswith(module.TRANSPORT_GAP_ISSUE)


def test_an_unreadable_verdict_store_yields_no_question(
    check_travel_bookings, monkeypatch, capsys, tmp_path
):
    """Widening the reader must not widen the alert-storm surface: a corrupt
    store still means no verdicts, never a question about every trip."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    (tmp_path / "drive-decisions.json").write_text("{not json", encoding="utf-8")

    _code, out, _err = _run(module, monkeypatch, capsys)
    assert json.loads(out)["gaps"] == []
    assert module.load_transport_gap_verdicts(tmp_path / "drive-decisions.json") == {}


def test_no_verdict_store_means_no_gap(check_travel_bookings, monkeypatch, capsys):
    """The pre-#231 behaviour, and the safe default: a drive trip has no
    transport booking by design, so silence beats nagging about every weekend
    away."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")

    _code, out, _err = _run(module, monkeypatch, capsys)
    assert json.loads(out)["gaps"] == []


@pytest.mark.parametrize(
    "payload",
    ["not json", json.dumps([1, 2]), json.dumps({"trips": {}})],
    ids=["unparseable", "not-an-object", "no-version"],
)
def test_a_corrupt_verdict_store_yields_no_gap_rather_than_a_storm(
    check_travel_bookings, monkeypatch, capsys, tmp_path, payload
):
    """A non-owner reader's no-prior-state path must never escalate work."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    (tmp_path / "drive-decisions.json").write_text(payload, encoding="utf-8")

    code, out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["gaps"] == []


def test_a_future_schema_version_yields_no_gap(
    check_travel_bookings, monkeypatch, capsys, tmp_path
):
    """This reader lags rather than migrates; it must not guess at a shape it
    does not know."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    _write_verdict(tmp_path, "fly", version=99)

    _code, out, _err = _run(module, monkeypatch, capsys)
    assert json.loads(out)["gaps"] == []


def test_a_fly_verdict_for_another_trip_does_not_leak(
    check_travel_bookings, monkeypatch, capsys, tmp_path
):
    """The join is on the trip key; a mismatched slug must not alert."""
    module, db_path, _state = check_travel_bookings
    db_path.write_text(json.dumps(_lodging_only_db()), encoding="utf-8")
    _write_verdict(tmp_path, "fly", slug="some-other-trip-2026-05")

    _code, out, _err = _run(module, monkeypatch, capsys)
    assert json.loads(out)["gaps"] == []


def test_a_flighted_trip_is_unaffected_by_the_verdict_store(
    check_travel_bookings, monkeypatch, capsys, tmp_path
):
    """The new branch only fires when transport is absent — a booked trip keeps
    its existing classification."""
    module, db_path, _state = check_travel_bookings
    db = _lodging_only_db()
    db["trips"][_TRIP_SLUG]["days"]["2026-05-15"].append(
        _item(type="Flight", summary="DL 123", start=date(2026, 5, 15), uid="item-3@tripit")
    )
    db_path.write_text(json.dumps(db), encoding="utf-8")
    _write_verdict(tmp_path, "fly")

    _code, out, _err = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert all(g["issue"] != "отель есть, рейса нет" for g in payload["gaps"])


# ---------------------------------------------------------------------------
# Red-eye home — local vs UTC dates (#268)
# ---------------------------------------------------------------------------


def _red_eye_home_trip(*, with_lodging):
    """A stay whose return is a red-eye: out on day 1, hotel through the
    last morning, and a flight that leaves at 11:05 PM local and lands the
    next morning. Every timed item carries both stamps, so a reader that
    reaches for the UTC one books the departure a day late.

    Trip window runs day 0 → day 7 (TripIt's exclusive end), the outbound is
    day 0, the hotel covers nights 0–4, and the red-eye leaves the night of
    day 5 to land on day 6.
    """
    day0 = _FROZEN_TODAY + timedelta(days=10)
    days = {
        day0.isoformat(): [
            {
                **_item(type="Flight", summary="DL891 BNA to SFO", start=f"{day0}T11:20:00Z"),
                "start_local": f"{day0}T06:20:00-05:00",
                "end_local": f"{day0}T08:30:00-07:00",
            },
        ],
    }
    if with_lodging:
        checkin = day0
        checkout = day0 + timedelta(days=5)
        days[checkin.isoformat()].append(
            {
                **_item(
                    type="Lodging",
                    summary="Check-in: Residence Inn",
                    start=f"{checkin}T22:00:00Z",
                    uid="item-in@tripit",
                ),
                "start_local": f"{checkin}T15:00:00-07:00",
            }
        )
        days[checkout.isoformat()] = [
            {
                **_item(
                    type="Lodging",
                    summary="Check-out: Residence Inn",
                    start=f"{checkout}T19:00:00Z",
                    uid="item-out@tripit",
                ),
                "start_local": f"{checkout}T12:00:00-07:00",
            }
        ]
    # The red-eye: 11:05 PM on day 5 local, 06:05Z on day 6.
    depart_local = day0 + timedelta(days=5)
    arrive_utc = day0 + timedelta(days=6)
    days.setdefault(depart_local.isoformat(), []).append(
        {
            **_item(
                type="Flight",
                summary="WN1683 SFO to BNA",
                start=f"{arrive_utc}T06:05:00Z",
                end=f"{arrive_utc}T10:30:00Z",
                uid="item-redeye@tripit",
            ),
            "start_local": f"{depart_local}T23:05:00-07:00",
            "end_local": f"{arrive_utc}T05:30:00-05:00",
        }
    )
    return _trip_record(
        summary="Onboarding CA",
        start=day0,
        end=day0 + timedelta(days=7),
        days=days,
    )


def test_night_spent_on_the_red_eye_is_not_a_hotel_gap(check_travel_bookings, monkeypatch, capsys):
    """The reported bug. The hotel checks out on the morning of day 5 and
    the traveller boards a red-eye that night, so day 5 is a night in the
    air, not a night owed a bed. Reading the departure off its UTC date
    files it on day 6 and leaves day 5 looking uncovered."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text(
        json.dumps(_db_payload({"onboarding-ca": _red_eye_home_trip(with_lodging=True)}))
    )

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["gaps"] == []
    assert output["complete_trips"] == 1


def test_red_eye_home_with_no_lodging_at_all_is_not_a_gap(
    check_travel_bookings, monkeypatch, capsys
):
    """A turnaround that flies out and takes the red-eye straight back
    spends no night on the ground, so it needs no hotel. Its arrival lands
    INSIDE the trip window rather than past it, which is why the arrival
    date alone cannot tell it from a trip the traveller is staying on."""
    module, db_path, _ = check_travel_bookings
    day0 = _FROZEN_TODAY + timedelta(days=10)
    trip = _trip_record(
        summary="Roast My PR",
        start=day0,
        end=day0 + timedelta(days=2),
        days={
            day0.isoformat(): [
                {
                    **_item(type="Flight", summary="DL750 BNA to SFO", start=f"{day0}T15:59:00Z"),
                    "start_local": f"{day0}T10:59:00-05:00",
                    "end_local": f"{day0}T15:42:00-07:00",
                },
                {
                    **_item(
                        type="Flight",
                        summary="DL690 SFO to BNA",
                        start=f"{day0 + timedelta(days=1)}T06:15:00Z",
                        end=f"{day0 + timedelta(days=1)}T12:00:00Z",
                        uid="item-redeye@tripit",
                    ),
                    "start_local": f"{day0}T23:15:00-07:00",
                    "end_local": f"{day0 + timedelta(days=1)}T08:30:00-05:00",
                },
            ],
        },
    )
    db_path.write_text(json.dumps(_db_payload({"roast-my-pr": trip})))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["gaps"] == []
    assert output["complete_trips"] == 1


def test_red_eye_home_does_not_excuse_uncovered_nights_before_it(
    check_travel_bookings, monkeypatch, capsys
):
    """How a trip ENDS says nothing about the nights in the middle of it.
    The same red-eye return with no lodging booked still owes a bed for
    every night the traveller was on the ground."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text(
        json.dumps(_db_payload({"onboarding-ca": _red_eye_home_trip(with_lodging=False)}))
    )

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert len(output["gaps"]) == 1
    gap = output["gaps"][0]
    assert gap["issue"] == "рейсы есть, отеля нет"
    # Nights 1–4: after the outbound day, before the night on the plane.
    day0 = _FROZEN_TODAY + timedelta(days=10)
    assert gap["uncovered_nights"] == [(day0 + timedelta(days=n)).isoformat() for n in range(1, 5)]


def test_v1_db_without_local_stamps_still_reads(check_travel_bookings, monkeypatch, capsys):
    """The DB on disk stays v1 until the next nightly rebuild. A record with
    no local stamps falls back to its UTC dates — the pre-#268 behavior —
    rather than erroring the whole brief during the rollout window."""
    module, db_path, _ = check_travel_bookings
    day0 = _FROZEN_TODAY + timedelta(days=10)
    payload = _db_payload(
        {
            "madrid": _trip_record(
                summary="Madrid",
                start=day0,
                end=day0 + timedelta(days=3),
                days={
                    day0.isoformat(): [
                        _item(type="Flight", summary="IB1 BNA to MAD", start=f"{day0}T11:20:00Z"),
                    ],
                    (day0 + timedelta(days=2)).isoformat(): [
                        _item(
                            type="Flight",
                            summary="IB2 MAD to BNA",
                            start=f"{day0 + timedelta(days=2)}T11:20:00Z",
                            uid="item-back@tripit",
                        ),
                    ],
                },
            )
        }
    )
    payload["schema_version"] = 1
    db_path.write_text(json.dumps(payload))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert len(output["gaps"]) == 1
    assert output["gaps"][0]["issue"] == "рейсы есть, отеля нет"
    assert output["gaps"][0]["uncovered_nights"] == [(day0 + timedelta(days=1)).isoformat()]


def test_v2_db_is_accepted(check_travel_bookings, monkeypatch, capsys):
    """The version the current writer stamps reads normally."""
    module, db_path, _ = check_travel_bookings
    payload = _db_payload({})
    payload["schema_version"] = 2
    db_path.write_text(json.dumps(payload))
    assert module.load_trips_from_db(str(db_path)) == []


def test_v3_db_is_accepted(check_travel_bookings):
    """The version the current writer stamps reads normally."""
    module, db_path, _ = check_travel_bookings
    payload = _db_payload({})
    payload["schema_version"] = 3
    db_path.write_text(json.dumps(payload))
    assert module.load_trips_from_db(str(db_path)) == []


# ---------------------------------------------------------------------------
# Home-metro placeholder suppression (#271)
# ---------------------------------------------------------------------------


def _write_home_metro(tmp_path, *metros):
    """Write the trusted profile's canonical `## Addresses` block carrying the
    given home-metro labels. The fixture already points USER_PROFILE_PATH here."""
    lines = "".join(f"- home_metro: {metro}\n" for metro in metros)
    (tmp_path / "user_profile.md").write_text(
        f"# Owner Profile\n\n## Addresses\n- home_airport: BNA\n{lines}",
        encoding="utf-8",
    )


def _empty_trip_payload(*, summary, destination=None):
    """A trip with an empty itinerary — the shape a TripIt placeholder takes.
    `classify_trip` reads it as `is_empty`, which fires the "nothing booked"
    gap unless the destination says the trip is local."""
    trip = _trip_record(
        summary=summary,
        start=_FROZEN_TODAY + timedelta(days=10),
        end=_FROZEN_TODAY + timedelta(days=11),
        days={},
    )
    if destination is not None:
        trip["destination"] = destination
    return _db_payload({"placeholder-2026-05": trip})


def test_home_metro_placeholder_raises_no_gap(check_travel_bookings, tmp_path, monkeypatch, capsys):
    """A local placeholder — a TripIt trip filed to block time for a Nashville
    event — has nothing to book. Its empty itinerary is the finished state."""
    module, db_path, _ = check_travel_bookings
    _write_home_metro(tmp_path, "Nashville, TN")
    db_path.write_text(
        json.dumps(_empty_trip_payload(summary="Alice's surgery", destination="Nashville, TN"))
    )

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["gaps"] == []
    assert output["local_trips"] == 1
    # Counted apart from `complete_trips`: nothing about it was checked.
    assert output["complete_trips"] == 0


def test_away_trip_with_nothing_booked_still_fires(
    check_travel_bookings, tmp_path, monkeypatch, capsys
):
    """The regression guard on the fix: an away trip with an empty itinerary is
    exactly what this check exists to catch, and looks identical to a local
    placeholder apart from its destination."""
    module, db_path, _ = check_travel_bookings
    _write_home_metro(tmp_path, "Nashville, TN")
    db_path.write_text(json.dumps(_empty_trip_payload(summary="Oslo", destination="Oslo, Norway")))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert [gap["issue"] for gap in output["gaps"]] == ["ничего не забукано"]
    assert output["local_trips"] == 0


def test_underway_empty_away_trip_raises_no_gap(
    check_travel_bookings, tmp_path, monkeypatch, capsys
):
    """#286: the live false positive. "Onboarding CA 2026 (Aug 17-24)" was
    flagged "nothing booked" on Aug 23 — the traveller had been in San
    Francisco for six days and was flying home the next day. Bookings were
    made out of band. Nagging about a trip that is all but over is noise;
    there is nothing left to book."""
    module, db_path, _ = check_travel_bookings
    _write_home_metro(tmp_path, "Nashville, TN")
    trip = _trip_record(
        summary="Onboarding CA 2026",
        start=_FROZEN_TODAY - timedelta(days=6),
        end=_FROZEN_TODAY + timedelta(days=1),
        days={},
    )
    trip["destination"] = "San Francisco, CA"
    db_path.write_text(json.dumps(_db_payload({"onboarding-ca-2026-08": trip})))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["gaps"] == []


def test_empty_away_trip_starting_today_raises_no_gap(
    check_travel_bookings, tmp_path, monkeypatch, capsys
):
    """The boundary: departure day. The window closes when the trip starts,
    not when it ends, so today's departure is already too late to book."""
    module, db_path, _ = check_travel_bookings
    _write_home_metro(tmp_path, "Nashville, TN")
    trip = _trip_record(
        summary="Oslo",
        start=_FROZEN_TODAY,
        end=_FROZEN_TODAY + timedelta(days=3),
        days={},
    )
    trip["destination"] = "Oslo, Norway"
    db_path.write_text(json.dumps(_db_payload({"oslo-2026": trip})))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["gaps"] == []


def test_unlabelled_trip_still_fires(check_travel_bookings, tmp_path, monkeypatch, capsys):
    """A trip the feed never labelled has an unknown destination, and unknown is
    never home — a pre-v3 DB must keep nagging, not go quiet."""
    module, db_path, _ = check_travel_bookings
    _write_home_metro(tmp_path, "Nashville, TN")
    db_path.write_text(json.dumps(_empty_trip_payload(summary="Somewhere")))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    assert [gap["issue"] for gap in json.loads(out)["gaps"]] == ["ничего не забукано"]


def test_no_home_metro_configured_checks_every_trip(check_travel_bookings, monkeypatch, capsys):
    """Absent `home_metro` in the profile is the pre-#271 behaviour: the local
    trip is nagged about, same as before the key existed."""
    module, db_path, _ = check_travel_bookings
    db_path.write_text(
        json.dumps(_empty_trip_payload(summary="Alice's surgery", destination="Nashville, TN"))
    )

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert [gap["issue"] for gap in output["gaps"]] == ["ничего не забукано"]
    assert output["local_trips"] == 0


def test_home_metro_suppresses_a_partially_booked_local_trip(
    check_travel_bookings, tmp_path, monkeypatch, capsys
):
    """Suppression is keyed on the destination alone, not on the itinerary being
    empty: a local trip that happens to carry a dinner reservation still needs
    no flight and no hotel."""
    module, db_path, _ = check_travel_bookings
    _write_home_metro(tmp_path, "Nashville, TN")
    trip_start = _FROZEN_TODAY + timedelta(days=10)
    payload = _db_payload(
        {
            "titans-2026-05": {
                **_trip_record(
                    summary="Jets at Titans",
                    start=trip_start,
                    end=trip_start + timedelta(days=2),
                    days={
                        trip_start.isoformat(): [
                            _item(type="Flight", summary="Outbound", start=trip_start),
                        ],
                    },
                ),
                "destination": "Nashville, TN",
            }
        }
    )
    db_path.write_text(json.dumps(payload))

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    output = json.loads(out)
    assert output["gaps"] == []
    assert output["local_trips"] == 1


def test_several_home_metro_labels_all_match(check_travel_bookings, tmp_path, monkeypatch, capsys):
    """The metro spans more than one label the feed might use; repeated
    `home_metro:` lines all count."""
    module, db_path, _ = check_travel_bookings
    _write_home_metro(tmp_path, "Nashville, TN", "Franklin, TN")
    db_path.write_text(
        json.dumps(_empty_trip_payload(summary="Local thing", destination="Franklin, TN"))
    )

    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["gaps"] == []
