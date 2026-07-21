"""Tests for skills/nightly-travel-sync/scripts/detect-new-trips.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - detect mode reads travel-db.json + travel-trips-seen.json and
    prints `{"seeded", "new_trips": [{slug, summary, start, end,
    log_line}]}` without writing anything
  - a missing / corrupt / wrong-version snapshot is a seed run:
    `seeded` true, `new_trips` empty — the itinerary is NOT dumped
  - after a snapshot exists, only slugs absent from it are new;
    output is sorted by (start, slug)
  - past trips (`end` before today) are excluded from both detection
    and the committed snapshot
  - `--commit` rewrites travel-trips-seen.json to exactly the current
    upcoming trip set (bounded, pruned) and prints `{"committed",
    "trips_tracked"}`
  - detect never mutates disk; commit is the sole writer
  - a missing DB exits 1 with an actionable stderr diagnostic
  - the seed → commit → detect cycle is idempotent (no re-log)
"""

import json
from datetime import date

_FROZEN_TODAY = date(2026, 6, 1)


def _make_frozen_date(real_date):
    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return _FROZEN_TODAY

    return FrozenDate


def _run(module, monkeypatch, capsys, argv, freeze=True):
    if freeze:
        monkeypatch.setattr(module, "date", _make_frozen_date(date))
    code = 0
    try:
        module.main(argv)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _trip(summary, start, end):
    return {"summary": summary, "start": start, "end": end, "days": {}}


def _write_db(db_path, trips):
    db_path.write_text(
        json.dumps({"schema_version": 1, "generated_at": "2026-06-01T06:00:00Z", "trips": trips})
    )


# A fixed itinerary: one upcoming trip, one already-past trip.
_MADRID = _trip("Madrid trip", "2026-06-10", "2026-06-15")
_PARIS = _trip("Paris trip", "2026-07-01", "2026-07-04")
_PAST = _trip("Old trip", "2026-01-01", "2026-01-05")


# --- seed run -------------------------------------------------------------


def test_absent_snapshot_is_seed_run(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    _write_db(db_path, {"madrid-trip-2026-06": _MADRID})
    code, out, _ = _run(module, monkeypatch, capsys, [])
    assert code == 0
    assert json.loads(out) == {"seeded": True, "new_trips": []}
    # detect writes nothing
    assert not seen_path.exists()


def test_corrupt_snapshot_is_seed_run(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    _write_db(db_path, {"madrid-trip-2026-06": _MADRID})
    seen_path.write_text("{ this is not valid json")
    code, out, _ = _run(module, monkeypatch, capsys, [])
    assert code == 0
    assert json.loads(out)["seeded"] is True


def test_unrecognised_version_is_seed_run(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    _write_db(db_path, {"madrid-trip-2026-06": _MADRID})
    seen_path.write_text(json.dumps({"schema_version": 99, "trips": {"x": {}}}))
    code, out, _ = _run(module, monkeypatch, capsys, [])
    assert code == 0
    assert json.loads(out)["seeded"] is True


# --- commit ---------------------------------------------------------------


def test_commit_writes_current_upcoming_set(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    _write_db(
        db_path,
        {
            "madrid-trip-2026-06": _MADRID,
            "old-trip-2026-01": _PAST,  # past — excluded
        },
    )
    code, out, _ = _run(module, monkeypatch, capsys, ["--commit"])
    assert code == 0
    assert json.loads(out) == {"committed": True, "trips_tracked": 1}
    snap = json.loads(seen_path.read_text())
    assert snap["schema_version"] == 1
    assert set(snap["trips"]) == {"madrid-trip-2026-06"}
    assert snap["trips"]["madrid-trip-2026-06"] == {
        "summary": "Madrid trip",
        "start": "2026-06-10",
        "end": "2026-06-15",
    }
    assert "generated_at" in snap


def test_commit_prunes_departed_trip(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    # A prior snapshot tracks two trips; the DB now has only one.
    seen_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-05-01T06:00:00Z",
                "trips": {
                    "madrid-trip-2026-06": {
                        "summary": "Madrid trip",
                        "start": "2026-06-10",
                        "end": "2026-06-15",
                    },
                    "gone-trip-2026-06": {
                        "summary": "Gone",
                        "start": "2026-06-20",
                        "end": "2026-06-22",
                    },
                },
            }
        )
    )
    _write_db(db_path, {"madrid-trip-2026-06": _MADRID})
    _run(module, monkeypatch, capsys, ["--commit"])
    snap = json.loads(seen_path.read_text())
    assert set(snap["trips"]) == {"madrid-trip-2026-06"}


# --- detection ------------------------------------------------------------


def _seed_snapshot(seen_path, slugs):
    seen_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-05-01T06:00:00Z",
                "trips": {
                    s: {"summary": s, "start": "2026-06-10", "end": "2026-06-15"} for s in slugs
                },
            }
        )
    )


def test_no_new_trips_when_snapshot_matches(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    _write_db(db_path, {"madrid-trip-2026-06": _MADRID})
    _seed_snapshot(seen_path, ["madrid-trip-2026-06"])
    code, out, _ = _run(module, monkeypatch, capsys, [])
    assert code == 0
    assert json.loads(out) == {"seeded": False, "new_trips": []}


def test_new_trip_detected_with_log_line(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    _write_db(
        db_path,
        {"madrid-trip-2026-06": _MADRID, "paris-trip-2026-07": _PARIS},
    )
    _seed_snapshot(seen_path, ["madrid-trip-2026-06"])
    code, out, _ = _run(module, monkeypatch, capsys, [])
    assert code == 0
    result = json.loads(out)
    assert result["seeded"] is False
    assert result["new_trips"] == [
        {
            "slug": "paris-trip-2026-07",
            "summary": "Paris trip",
            "start": "2026-07-01",
            "end": "2026-07-04",
            "log_line": "[travel] new trip: Paris trip (2026-07-01 to 2026-07-04)",
        }
    ]
    # log_line carries no timestamp — the caller prepends `- HH:MM UTC`.
    assert not result["new_trips"][0]["log_line"].startswith("-")


def test_multiple_new_trips_sorted_by_start(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    _write_db(
        db_path,
        {
            "paris-trip-2026-07": _PARIS,
            "madrid-trip-2026-06": _MADRID,
        },
    )
    _seed_snapshot(seen_path, [])  # empty prior set: both are new
    _, out, _ = _run(module, monkeypatch, capsys, [])
    slugs = [t["slug"] for t in json.loads(out)["new_trips"]]
    assert slugs == ["madrid-trip-2026-06", "paris-trip-2026-07"]


def test_past_trip_never_new(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    # The past trip is absent from the (empty) snapshot but must not
    # surface as new — it is outside the upcoming window.
    _write_db(db_path, {"old-trip-2026-01": _PAST})
    _seed_snapshot(seen_path, [])
    _, out, _ = _run(module, monkeypatch, capsys, [])
    assert json.loads(out)["new_trips"] == []


def test_detect_does_not_write_snapshot(detect_new_trips, monkeypatch, capsys):
    module, db_path, seen_path = detect_new_trips
    _write_db(db_path, {"paris-trip-2026-07": _PARIS})
    _seed_snapshot(seen_path, ["madrid-trip-2026-06"])
    before = seen_path.read_text()
    _run(module, monkeypatch, capsys, [])
    assert seen_path.read_text() == before


# --- error handling -------------------------------------------------------


def test_missing_db_exits_1(detect_new_trips, monkeypatch, capsys):
    module, db_path, _ = detect_new_trips
    assert not db_path.exists()
    code, _, err = _run(module, monkeypatch, capsys, [])
    assert code == 1
    assert str(db_path) in err
    assert "build-travel-db.py" in err


def test_corrupt_db_exits_1(detect_new_trips, monkeypatch, capsys):
    module, db_path, _ = detect_new_trips
    db_path.write_text("{ truncated")
    code, _, err = _run(module, monkeypatch, capsys, [])
    assert code == 1
    assert "build-travel-db.py" in err


def test_wrong_root_shape_db_exits_1(detect_new_trips, monkeypatch, capsys):
    module, db_path, _ = detect_new_trips
    db_path.write_text(json.dumps(["not", "an", "object"]))
    code, _, err = _run(module, monkeypatch, capsys, [])
    assert code == 1
    assert "trips" in err


def test_unknown_argument_exits_2(detect_new_trips, monkeypatch, capsys):
    module, _, _ = detect_new_trips
    code, _, err = _run(module, monkeypatch, capsys, ["--bogus"])
    assert code == 2
    assert "usage" in err


# --- end-to-end idempotency ----------------------------------------------


def test_seed_commit_detect_cycle_is_idempotent(detect_new_trips, monkeypatch, capsys):
    module, db_path, _ = detect_new_trips
    _write_db(db_path, {"madrid-trip-2026-06": _MADRID})

    # Seed run: nothing new.
    _, out, _ = _run(module, monkeypatch, capsys, [])
    assert json.loads(out) == {"seeded": True, "new_trips": []}
    _run(module, monkeypatch, capsys, ["--commit"])

    # Next run, same DB: still nothing new.
    _, out, _ = _run(module, monkeypatch, capsys, [])
    assert json.loads(out) == {"seeded": False, "new_trips": []}

    # A new trip appears.
    _write_db(
        db_path,
        {"madrid-trip-2026-06": _MADRID, "paris-trip-2026-07": _PARIS},
    )
    _, out, _ = _run(module, monkeypatch, capsys, [])
    new = json.loads(out)["new_trips"]
    assert [t["slug"] for t in new] == ["paris-trip-2026-07"]
    _run(module, monkeypatch, capsys, ["--commit"])

    # After commit, the same trip is no longer new.
    _, out, _ = _run(module, monkeypatch, capsys, [])
    assert json.loads(out)["new_trips"] == []
