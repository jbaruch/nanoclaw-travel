"""Tests for source normalization (byAir record / TripIt segment → Flight).

Deterministic fixtures only — hand-built records matching the documented shapes
(flight-<id>.json per state-schema.md; travel-schedule.json Flight segment), no
wall-clock. These pin: byAir carries scheduled + live times (byAir wins), TripIt
carries scheduled only, both key on caller-resolved IATA codes, and required
fields are enforced.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from flight_identity import BYAIR, TRIPIT  # noqa: E402
from normalize import flight_from_byair, flight_from_tripit_segment  # noqa: E402

UTC = timezone.utc


def _byair_record(**over):
    record = {
        "schema_version": 6,
        "flight_id": 6277117,
        "code": "FR7382",
        "trip_id": 678,
        "scheduled_dep_time": "2026-07-12T09:00:00+00:00",
        "scheduled_arr_time": "2026-07-12T11:05:00+00:00",
        "dep_airport_id": 20,
        "arr_airport_id": 28,
        "last_snapshot": {"dep_time": "2026-07-12T09:35:00+00:00", "arr_time": None},
    }
    record.update(over)
    return record


def test_byair_scheduled_and_live_times():
    f = flight_from_byair(_byair_record(), dep_iata="STN", arr_iata="CPH")
    assert f.source == BYAIR
    assert f.dep_airport == "STN" and f.arr_airport == "CPH"
    assert f.scheduled_dep == datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    assert f.scheduled_arr == datetime(2026, 7, 12, 11, 5, tzinfo=UTC)
    assert f.live_dep == datetime(2026, 7, 12, 9, 35, tzinfo=UTC)
    assert f.live_arr is None
    assert f.byair_flight_id == 6277117
    assert f.code == "FR7382"
    assert f.trip_id == 678


def test_byair_zulu_suffix_parsed():
    f = flight_from_byair(
        _byair_record(scheduled_dep_time="2026-07-12T09:00:00Z"), dep_iata="STN", arr_iata="CPH"
    )
    assert f.scheduled_dep == datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def test_byair_missing_snapshot_is_fine():
    f = flight_from_byair(_byair_record(last_snapshot=None), dep_iata="STN", arr_iata="CPH")
    assert f.live_dep is None and f.live_arr is None


def test_byair_requires_int_flight_id():
    with pytest.raises(ValueError, match="int flight_id"):
        flight_from_byair(_byair_record(flight_id="6277117"), dep_iata="STN", arr_iata="CPH")


def test_byair_requires_scheduled_dep():
    with pytest.raises(ValueError, match="scheduled_dep_time"):
        flight_from_byair(_byair_record(scheduled_dep_time=None), dep_iata="STN", arr_iata="CPH")


# --- TripIt -----------------------------------------------------------------


def _tripit_segment(**over):
    seg = {
        "schema_version": 1,
        "type": "Flight",
        "uid": "uid-flight-1",
        "summary": "FR 7382 STN to CPH",
        "start": "2026-07-12T09:00:00Z",
        "end": "2026-07-12T11:05:00Z",
        "location": "London Stansted Airport (STN)",
    }
    seg.update(over)
    return seg


def test_tripit_scheduled_only():
    f = flight_from_tripit_segment(_tripit_segment(), dep_iata="STN", arr_iata="CPH", code="FR7382")
    assert f.source == TRIPIT
    assert f.dep_airport == "STN" and f.arr_airport == "CPH"
    assert f.scheduled_dep == datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    assert f.scheduled_arr == datetime(2026, 7, 12, 11, 5, tzinfo=UTC)
    assert f.live_dep is None and f.live_arr is None
    assert f.tripit_segment_id == "uid-flight-1"
    assert f.code == "FR7382"


def test_tripit_rejects_non_flight_segment():
    with pytest.raises(ValueError, match="not a Flight segment"):
        flight_from_tripit_segment(_tripit_segment(type="Lodging"), dep_iata="STN", arr_iata="CPH")


def test_tripit_requires_uid():
    with pytest.raises(ValueError, match="uid"):
        flight_from_tripit_segment(_tripit_segment(uid=None), dep_iata="STN", arr_iata="CPH")


def test_tripit_requires_start():
    with pytest.raises(ValueError, match="start"):
        flight_from_tripit_segment(
            _tripit_segment(start="not-a-date"), dep_iata="STN", arr_iata="CPH"
        )
