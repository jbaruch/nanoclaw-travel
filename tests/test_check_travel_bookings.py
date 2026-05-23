"""Baseline tests for skills/check-travel-bookings/scripts/check-travel-bookings.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - DB-only flow: `travel-db.json` is the sole input. A missing,
    unreadable, or structurally invalid file is a hard error
    (`{"error": "..."}` + exit 1) — no live-ICS fallback. The
    alerting surface is `nightly-external-sync` Step 5's failure
    branch (notify + scheduled continuation), not the Step 4 probe
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
  - `build_lodging_ranges` pairs `Check-in:` / `Check-out:` events by
    hotel name; an orphan check-in defaults to a 1-day stay
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
from datetime import date, datetime, timedelta, timezone

_FROZEN_TODAY = date(2026, 4, 30)
_FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


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


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_make_slug_format(check_travel_bookings):
    """`make_slug` lowercases + dashifies + strips trailing year +
    appends YYYY-MM."""
    module, *_ = check_travel_bookings
    slug = module.make_slug("Madrid Tech Days 2026", date(2026, 6, 15))
    assert slug == "madrid-tech-days-2026-06"


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


# ---------------------------------------------------------------------------
# classify_trip branches
# ---------------------------------------------------------------------------


def test_classify_trip_empty(check_travel_bookings):
    module, *_ = check_travel_bookings
    out = module.classify_trip(
        items=[], trip_start=_FROZEN_TODAY, trip_end=_FROZEN_TODAY + timedelta(days=3)
    )
    assert out["is_empty"] is True
    assert out["has_transport"] is False


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
    out = module.classify_trip(items, trip_start, trip_end)
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
    out = module.classify_trip(items, trip_start, trip_end)
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
    out = module.classify_trip(items, trip_start, trip_end)
    # No future transport after night 0 → no uncovered flagged.
    assert out["uncovered_nights"] == []


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
    `nightly-external-sync`, not here."""
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
    diagnostic on stderr, exit 1. The Step 5 rebuild failure branch
    in `nightly-external-sync` is the alerting surface for DB issues,
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
    payload["schema_version"] = 2
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
                    "schema_version": 2,
                    "snooze_until": (_FROZEN_TODAY + timedelta(days=2)).isoformat(),
                }
            }
        )
    )

    _, out, _ = _run(module, monkeypatch, capsys)
    output = json.loads(out)
    assert len(output["gaps"]) == 1
    assert output["gaps"][0]["slug"] == "madrid-2026-06"


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
