"""Tests for skills/flight-assist/trip_window.py — the #147 defense-in-depth.

Deterministic: a fixed injected `now`, fixed fixture trip dates, and a tmp
travel-db.json per case. Mirrors the host spawn-gate's window cases
(jbaruch/nanoclaw#754 `src/spawn-gates.test.ts`) so the two layers stay aligned.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from trip_window import evaluate_trip_window  # noqa: E402

# A fixed reference instant for every case (UTC) — a fixed PAST date so the
# suite never depends on the run date (testing-standards).
NOW = datetime(2020, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def _write_db(tmp_path: Path, payload) -> str:
    p = tmp_path / "travel-db.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _db(trips: dict) -> dict:
    return {"schema_version": 1, "generated_at": "2020-07-01T00:00:00Z", "trips": trips}


# --- in / out of window -----------------------------------------------------


def test_now_inside_a_trip_is_in_window(tmp_path: Path):
    path = _write_db(tmp_path, _db({"osl": {"start": "2020-07-15", "end": "2020-07-20"}}))
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is True


def test_trip_with_z_suffixed_iso_dates_is_parsed(tmp_path: Path):
    """A `Z`-stamped full-ISO date is normalized and evaluated, not skipped — a
    future travel-db writer using `Z` must never fall out of window mid-trip."""
    path = _write_db(
        tmp_path,
        _db({"osl": {"start": "2020-07-15T00:00:00Z", "end": "2020-07-20T00:00:00Z"}}),
    )
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is True


def test_future_trip_beyond_24h_lead_is_out(tmp_path: Path):
    # Trip starts 2020-07-17 → window opens 2020-07-16T00:00Z, after NOW.
    path = _write_db(tmp_path, _db({"osl": {"start": "2020-07-17", "end": "2020-07-20"}}))
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is False


def test_past_trip_beyond_24h_trail_is_out(tmp_path: Path):
    # Trip ended 2020-07-13 → window closes 2020-07-14T00:00Z, before NOW.
    path = _write_db(tmp_path, _db({"osl": {"start": "2020-07-08", "end": "2020-07-13"}}))
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is False


# --- boundary conditions (24h lead is inclusive, end+24h is exclusive) -------


def test_exactly_24h_before_start_is_in_window(tmp_path: Path):
    # start 2020-07-16 → window opens exactly 2020-07-15T00:00Z.
    at_open = datetime(2020, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
    path = _write_db(tmp_path, _db({"osl": {"start": "2020-07-16", "end": "2020-07-20"}}))
    assert evaluate_trip_window(now_utc=at_open, path=path).in_window is True


def test_one_second_before_24h_lead_is_out(tmp_path: Path):
    just_before = datetime(2020, 7, 14, 23, 59, 59, tzinfo=timezone.utc)
    path = _write_db(tmp_path, _db({"osl": {"start": "2020-07-16", "end": "2020-07-20"}}))
    assert evaluate_trip_window(now_utc=just_before, path=path).in_window is False


def test_last_day_before_end_plus_24h_is_in_window(tmp_path: Path):
    # end 2020-07-14 → window closes 2020-07-15T00:00Z; 23:59:59 the day before is in.
    late_final_day = datetime(2020, 7, 14, 23, 59, 59, tzinfo=timezone.utc)
    path = _write_db(tmp_path, _db({"osl": {"start": "2020-07-10", "end": "2020-07-14"}}))
    assert evaluate_trip_window(now_utc=late_final_day, path=path).in_window is True


def test_exactly_end_plus_24h_is_out(tmp_path: Path):
    # end 2020-07-14 → window closes exactly 2020-07-15T00:00Z (exclusive).
    at_close = datetime(2020, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
    path = _write_db(tmp_path, _db({"osl": {"start": "2020-07-10", "end": "2020-07-14"}}))
    assert evaluate_trip_window(now_utc=at_close, path=path).in_window is False


# --- union over multiple trips ----------------------------------------------


def test_now_in_one_of_several_trips_is_in_window(tmp_path: Path):
    path = _write_db(
        tmp_path,
        _db(
            {
                "past": {"start": "2020-06-01", "end": "2020-06-05"},
                "current": {"start": "2020-07-14", "end": "2020-07-18"},
                "future": {"start": "2020-09-01", "end": "2020-09-05"},
            }
        ),
    )
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is True


# --- fail semantics (asymmetric, matching the host) -------------------------


def test_absent_travel_db_is_out_of_window(tmp_path: Path):
    missing = str(tmp_path / "travel-db.json")  # never written
    result = evaluate_trip_window(now_utc=NOW, path=missing)
    assert result.in_window is False
    assert "no travel itinerary" in result.reason


def test_corrupt_json_fails_open(tmp_path: Path):
    p = tmp_path / "travel-db.json"
    p.write_text("{not valid json", encoding="utf-8")
    result = evaluate_trip_window(now_utc=NOW, path=str(p))
    assert result.in_window is True  # fail OPEN — never blind an active trip
    assert "failing open" in result.reason


def test_missing_trips_key_fails_open(tmp_path: Path):
    path = _write_db(tmp_path, {"schema_version": 1, "generated_at": "x"})
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is True


def test_trips_wrong_shape_fails_open(tmp_path: Path):
    path = _write_db(tmp_path, {"schema_version": 1, "trips": ["not", "a", "map"]})
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is True


def test_unreadable_path_fails_open(tmp_path: Path):
    # A directory at the travel-db path → read raises OSError → fail open.
    d = tmp_path / "travel-db.json"
    d.mkdir()
    assert evaluate_trip_window(now_utc=NOW, path=str(d)).in_window is True


# --- non-owner reader schema_version gate (stateful-artifacts) --------------


def test_unaccepted_schema_version_fails_open(tmp_path: Path):
    """A travel-db stamped with a version this reader doesn't accept is treated
    as no-usable-state → fail OPEN, overriding an otherwise out-of-window trip,
    so a cross-pipeline schema bump never blinds an active trip."""
    path = _write_db(
        tmp_path,
        {
            "schema_version": 2,  # ahead of the accepted 1
            "trips": {"past": {"start": "2020-01-01", "end": "2020-01-05"}},
        },
    )
    result = evaluate_trip_window(now_utc=NOW, path=path)
    assert result.in_window is True
    assert "schema_version" in result.reason


def test_absent_schema_version_is_read_as_v1(tmp_path: Path):
    """A record with no schema_version is legacy-implicit v1 — evaluated
    normally (not failed open): an out-of-window trip stays out of window."""
    path = _write_db(tmp_path, {"trips": {"past": {"start": "2020-01-01", "end": "2020-01-05"}}})
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is False


# --- valid-but-empty and unparseable-date handling --------------------------


def test_empty_trip_map_is_out_of_window(tmp_path: Path):
    path = _write_db(tmp_path, _db({}))
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is False


def test_trip_with_unparseable_dates_is_skipped(tmp_path: Path):
    # The broken trip is ignored; the valid trip covering NOW still wins.
    path = _write_db(
        tmp_path,
        _db(
            {
                "broken": {"start": "not-a-date", "end": "also-bad"},
                "valid": {"start": "2020-07-14", "end": "2020-07-18"},
            }
        ),
    )
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is True


def test_only_unparseable_trip_is_out_of_window(tmp_path: Path):
    path = _write_db(tmp_path, _db({"broken": {"start": "nope", "end": "nope"}}))
    assert evaluate_trip_window(now_utc=NOW, path=path).in_window is False
