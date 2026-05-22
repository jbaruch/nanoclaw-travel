"""Baseline tests for check-travel-bookings/scripts/build-travel-db.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Reads `travel-schedule.json` (a flat event list); writes
    `travel-db.json` with a `{generated_at, trips: {<slug>: {...}}}`
    shape
  - Trips (events without `item-` in `uid`) are kept iff their `end`
    is on/after today; items overlap into the trip's days bucket
  - Each day's items are sorted by `TYPE_ORDER` (Flight, Rail,
    Lodging, Car Rental, then alphabetic)
  - Exit 1 on missing schedule (with stderr diagnostic naming the
    expected path)
"""

import json
from datetime import date

_FROZEN_TODAY = date(2026, 4, 30)


def _make_frozen_date(real_date):
    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return _FROZEN_TODAY

    return FrozenDate


def _run(module, monkeypatch, capsys, freeze=True):
    monkeypatch.setattr("sys.argv", ["build-travel-db.py"])
    if freeze:
        monkeypatch.setattr(module, "date", _make_frozen_date(date))
    code = 0
    try:
        module.main()
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _trip(uid, summary, start, end):
    return {
        "uid": uid,
        "summary": summary,
        "start": start,
        "end": end,
        "type": "Trip",
    }


def _item(uid, summary, start, end, item_type):
    return {
        "uid": uid,
        "summary": summary,
        "start": start,
        "end": end,
        "type": item_type,
    }


def test_missing_schedule_exits_1(build_travel_db, monkeypatch, capsys):
    module, schedule_path, _ = build_travel_db
    assert not schedule_path.exists()
    code, _, err = _run(module, monkeypatch, capsys)
    assert code == 1
    assert str(schedule_path) in err
    assert "run refresh-travel-schedule.py first" in err


def test_writes_db_with_trips_and_items(build_travel_db, monkeypatch, capsys):
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip-1", "Boston Conf", "2026-05-10", "2026-05-13"),
        _item("item-1", "ATL→BOS", "2026-05-10", "2026-05-10", "Flight"),
        _item("item-2", "Hilton Boston", "2026-05-10", "2026-05-13", "Lodging"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    assert "generated_at" in db
    assert len(db["trips"]) == 1
    slug = next(iter(db["trips"]))
    trip = db["trips"][slug]
    assert trip["summary"] == "Boston Conf"
    assert trip["start"] == "2026-05-10"
    # Item lands in its start-day bucket
    assert "2026-05-10" in trip["days"]


def test_past_trip_excluded(build_travel_db, monkeypatch, capsys):
    """Trip ending before frozen-today is dropped."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("old-trip", "Old Trip", "2026-04-01", "2026-04-05"),
        _trip("future-trip", "Future Trip", "2026-06-10", "2026-06-12"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    _run(module, monkeypatch, capsys)
    db = json.loads(db_path.read_text())
    assert len(db["trips"]) == 1
    # Assert on the surviving trip's *value* (summary, start, end)
    # rather than the slug string — slug derivation isn't part of the
    # documented contract.
    surviving = next(iter(db["trips"].values()))
    assert surviving["summary"] == "Future Trip"
    assert surviving["start"] == "2026-06-10"
    assert surviving["end"] == "2026-06-12"


def test_items_sorted_by_type_within_day(build_travel_db, monkeypatch, capsys):
    """Multiple items on the same day sort: Flight < Rail < Lodging < Car Rental."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Multiday", "2026-05-10", "2026-05-13"),
        _item("item-c", "Hertz", "2026-05-10", "2026-05-10", "Car Rental"),
        _item("item-l", "Hotel", "2026-05-10", "2026-05-12", "Lodging"),
        _item("item-f", "Flight 1", "2026-05-10", "2026-05-10", "Flight"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    _run(module, monkeypatch, capsys)
    db = json.loads(db_path.read_text())
    slug = next(iter(db["trips"]))
    day = db["trips"][slug]["days"]["2026-05-10"]
    types = [e["type"] for e in day]
    assert types == ["Flight", "Lodging", "Car Rental"]


def test_overlapping_items_distributed_to_start_day(build_travel_db, monkeypatch, capsys):
    """Items get bucketed by their `start` date, even when they span days."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Trip", "2026-05-10", "2026-05-15"),
        _item("item-l", "Hotel", "2026-05-11", "2026-05-14", "Lodging"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    _run(module, monkeypatch, capsys)
    db = json.loads(db_path.read_text())
    slug = next(iter(db["trips"]))
    days = db["trips"][slug]["days"]
    assert "2026-05-11" in days  # bucketed at start date
    assert "2026-05-12" not in days


def test_unknown_type_kept_with_default_order(build_travel_db, monkeypatch, capsys):
    """A novel item type (TripIt adds a new category) sorts after the
    known types per the `TYPE_ORDER` dict's default of 9."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Trip", "2026-05-10", "2026-05-12"),
        _item("item-novel", "ZebraBoat", "2026-05-10", "2026-05-10", "Boat"),
        _item("item-flight", "Flight", "2026-05-10", "2026-05-10", "Flight"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    _run(module, monkeypatch, capsys)
    db = json.loads(db_path.read_text())
    slug = next(iter(db["trips"]))
    types = [e["type"] for e in db["trips"][slug]["days"]["2026-05-10"]]
    # Flight (order 0) before Boat (default order 9)
    assert types == ["Flight", "Boat"]


def test_timed_item_buckets_by_date_and_preserves_time(build_travel_db, monkeypatch, capsys):
    """Timed-item shape from `refresh-travel-schedule.py` post-`nanoclaw-admin#289`
    (`start`/`end` as `YYYY-MM-DDTHH:MM:SSZ`) parses without error,
    buckets under the calendar-date `day_key`, and propagates the full
    ISO-datetime string into the per-day record so downstream consumers
    (flight-assist, the `time_to_leave` precheck) can read the departure
    time without going back to TripIt."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Munich Trip", "2026-05-21", "2026-05-23"),
        _item(
            "item-flight", "DL23 MUC→DTW", "2026-05-22T07:00:00Z", "2026-05-22T14:00:00Z", "Flight"
        ),
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    slug = next(iter(db["trips"]))
    days = db["trips"][slug]["days"]
    assert "2026-05-22" in days
    flight = days["2026-05-22"][0]
    assert flight["start"] == "2026-05-22T07:00:00Z"
    assert flight["end"] == "2026-05-22T14:00:00Z"
