"""Tests for skills/flight-assist/trip_origin.py (#122).

Locks the anchor-resolution contract:

  - `load_travel_schedule` is a tolerant non-owner reader: missing /
    corrupt / non-UTF-8 / non-list-root / forward-incompatible files all
    resolve to None (static-home behavior), never an exception
  - `resolve_anchor` rules: off-trip → home; on-trip → latest Lodging
    event (check-in OR check-out) within the trip span at or before the
    anchor time; pre-first-lodging → the Trip's own location; nothing →
    unresolved (address None). Home is NEVER the anchor mid-trip
  - the live #122 case: mid-gap between check-out and next check-in the
    prior stay's lodging wins
  - `resolve_effective_home` is the I/O convenience over both

Fixtures mirror the record shape refresh-travel-schedule.py writes (flat
list, `YYYY-MM-DDTHH:MM:SSZ` timed / `YYYY-MM-DD` date-only, address in
`location`) with synthetic venues; dates are fixed per
`coding-policy: testing-standards` (Determinism).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

import trip_origin  # noqa: E402
from trip_origin import (  # noqa: E402
    SCHEDULE_SCHEMA_VERSION,
    TripAnchor,
    load_travel_schedule,
    resolve_anchor,
    resolve_effective_home,
)

HOME = "12 Example St, Sampleton, TN 37000"
AIRBNB = "1 Seaside Lane, Hastings, UK"
AIRPORT_HOTEL = "Thremhall Ave, Stansted, UK"


def _record(
    *,
    type: str,
    summary: str,
    start: str,
    end: str,
    location: str | None = None,
    schema_version: int | None = SCHEDULE_SCHEMA_VERSION,
) -> dict:
    record = {
        "summary": summary,
        "start": start,
        "end": end,
        "location": location,
        "type": type,
        "uid": f"uid-{summary.lower().replace(' ', '-').replace(':', '')}",
    }
    if schema_version is not None:
        record["schema_version"] = schema_version
    return record


def _uk_trip_schedule() -> list[dict]:
    """The #122 shape: a UK trip with two consecutive stays.

    Mirrors the live 2026 incident data shifted one year into the past —
    fixtures stay fixed PAST dates per `coding-policy: testing-standards`
    (no hardcoded future dates that a later run date could interact with).
    """
    return [
        _record(
            type="Trip",
            summary="Scotland + UK offsite 2025",
            start="2025-06-26",
            end="2025-07-13",
            location="United Kingdom",
        ),
        _record(
            type="Flight",
            summary="BNA to LHR",
            start="2025-06-26T19:00:00Z",
            end="2025-06-27T07:30:00Z",
            location="Nashville International Airport",
        ),
        _record(
            type="Lodging",
            summary="Check-in: Airbnb - Jane",
            start="2025-07-06T15:00:00Z",
            end="2025-07-06T16:00:00Z",
            location=AIRBNB,
        ),
        _record(
            type="Lodging",
            summary="Check-out: Airbnb - Jane",
            start="2025-07-11T10:00:00Z",
            end="2025-07-11T11:00:00Z",
            location=AIRBNB,
        ),
        _record(
            type="Lodging",
            summary="Check-in: Hampton by Hilton London Stansted Airport",
            start="2025-07-11T14:00:00Z",
            end="2025-07-11T15:00:00Z",
            location=AIRPORT_HOTEL,
        ),
    ]


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# load_travel_schedule tolerance
# ---------------------------------------------------------------------------


def test_load_missing_file_returns_none(tmp_path):
    assert load_travel_schedule(str(tmp_path / "travel-schedule.json")) is None


def test_load_corrupt_json_returns_none(tmp_path):
    path = tmp_path / "travel-schedule.json"
    path.write_text("[{not json")
    assert load_travel_schedule(str(path)) is None


def test_load_non_utf8_returns_none(tmp_path):
    path = tmp_path / "travel-schedule.json"
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert load_travel_schedule(str(path)) is None


def test_load_non_list_root_returns_none(tmp_path):
    path = tmp_path / "travel-schedule.json"
    path.write_text(json.dumps({"events": []}))
    assert load_travel_schedule(str(path)) is None


def test_load_forward_incompatible_version_returns_none(tmp_path):
    """Any record carrying a HIGHER schema_version marks this reader as
    lagging — the whole file takes the no-usable-schedule path per
    `coding-policy: stateful-artifacts` (non-owner readers never guess)."""
    schedule = _uk_trip_schedule()
    schedule[0]["schema_version"] = SCHEDULE_SCHEMA_VERSION + 1
    path = tmp_path / "travel-schedule.json"
    path.write_text(json.dumps(schedule))
    assert load_travel_schedule(str(path)) is None


def test_load_accepts_current_and_legacy_versions(tmp_path):
    """v1 records and legacy records with no schema_version both read."""
    schedule = _uk_trip_schedule()
    del schedule[0]["schema_version"]  # legacy record
    path = tmp_path / "travel-schedule.json"
    path.write_text(json.dumps(schedule))
    loaded = load_travel_schedule(str(path))
    assert loaded is not None
    assert len(loaded) == len(schedule)


def test_load_drops_non_dict_entries(tmp_path):
    path = tmp_path / "travel-schedule.json"
    path.write_text(json.dumps([*_uk_trip_schedule(), "stray-string", 42]))
    loaded = load_travel_schedule(str(path))
    assert loaded is not None
    assert all(isinstance(record, dict) for record in loaded)


# ---------------------------------------------------------------------------
# resolve_anchor rules
# ---------------------------------------------------------------------------


def test_naive_at_raises():
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_anchor(None, at=datetime(2025, 7, 7, 12, 0), home_address=HOME)


def test_no_schedule_resolves_home():
    anchor = resolve_anchor(None, at=_at("2025-07-07T12:00:00Z"), home_address=HOME)
    assert anchor == TripAnchor(address=HOME, source="home")


def test_off_trip_resolves_home():
    anchor = resolve_anchor(_uk_trip_schedule(), at=_at("2025-07-20T12:00:00Z"), home_address=HOME)
    assert anchor.address == HOME
    assert anchor.source == "home"


def test_on_trip_after_checkin_resolves_that_lodging():
    """The #122 headline: a UK dinner while lodged at the Airbnb anchors
    at the Airbnb, never the Tennessee residence."""
    anchor = resolve_anchor(_uk_trip_schedule(), at=_at("2025-07-07T18:00:00Z"), home_address=HOME)
    assert anchor.address == AIRBNB
    assert anchor.source == "lodging"


def test_checkout_to_checkin_gap_keeps_prior_lodging():
    """The issue's verified live case (2026-07-11 12:00Z, fixture shifted a
    year into the past): between the Airbnb check-out 10:00Z and the
    Hampton check-in 14:00Z the latest lodging event ≤ T is the check-out,
    so the Airbnb wins."""
    anchor = resolve_anchor(_uk_trip_schedule(), at=_at("2025-07-11T12:00:00Z"), home_address=HOME)
    assert anchor.address == AIRBNB
    assert anchor.source == "lodging"


def test_after_next_checkin_switches_lodging():
    anchor = resolve_anchor(_uk_trip_schedule(), at=_at("2025-07-11T20:00:00Z"), home_address=HOME)
    assert anchor.address == AIRPORT_HOTEL
    assert anchor.source == "lodging"


def test_pre_first_lodging_falls_back_to_trip_location_not_home():
    """A meeting before the trip's first lodging event anchors at the
    Trip's own location — home is NEVER the anchor mid-trip."""
    anchor = resolve_anchor(_uk_trip_schedule(), at=_at("2025-06-28T12:00:00Z"), home_address=HOME)
    assert anchor.address == "United Kingdom"
    assert anchor.source == "trip_location"


def test_pre_first_lodging_without_trip_location_is_unresolved():
    schedule = _uk_trip_schedule()
    schedule[0]["location"] = None
    anchor = resolve_anchor(schedule, at=_at("2025-06-28T12:00:00Z"), home_address=HOME)
    assert anchor.address is None
    assert anchor.source == "unresolved"
    assert anchor.detail is not None and "no lodging" in anchor.detail


def test_prior_trip_lodging_outside_span_is_excluded():
    """A straggler check-out from an earlier trip (retained by the
    refresh's live-stay pairing) must not anchor this trip's meetings in
    the wrong city — lodging candidates are bounded to the active trip's
    span, so this falls through to the trip location."""
    schedule = [
        _record(
            type="Lodging",
            summary="Check-out: Old City Hotel",
            start="2025-06-20T10:00:00Z",
            end="2025-06-20T11:00:00Z",
            location="9 Elsewhere Sq, Old City",
        ),
        *_uk_trip_schedule(),
    ]
    anchor = resolve_anchor(schedule, at=_at("2025-06-28T12:00:00Z"), home_address=HOME)
    assert anchor.address == "United Kingdom"
    assert anchor.source == "trip_location"


def test_lodging_with_blank_location_is_skipped():
    schedule = _uk_trip_schedule()
    for record in schedule:
        if record["type"] == "Lodging":
            record["location"] = "  "
    anchor = resolve_anchor(schedule, at=_at("2025-07-07T18:00:00Z"), home_address=HOME)
    assert anchor.source == "trip_location"


def test_trip_span_boundary_days_are_on_trip():
    """Both endpoint dates of the date-only Trip wrapper count as
    traveling — the safe direction for the #122 failure mode."""
    for boundary in ("2025-06-26T02:00:00Z", "2025-07-13T22:00:00Z"):
        anchor = resolve_anchor(_uk_trip_schedule(), at=_at(boundary), home_address=HOME)
        assert anchor.source != "home", boundary


def test_off_trip_none_home_is_none_with_home_source():
    """flight-assist may have no configured home_address; off-trip that
    stays the callers' existing no-origin contract."""
    anchor = resolve_anchor(_uk_trip_schedule(), at=_at("2025-07-20T12:00:00Z"), home_address=None)
    assert anchor.address is None
    assert anchor.source == "home"


def test_overlapping_trips_latest_start_wins():
    schedule = [
        *_uk_trip_schedule(),
        _record(
            type="Trip",
            summary="Nested side trip",
            start="2025-07-05",
            end="2025-07-08",
            location="Edinburgh, UK",
        ),
    ]
    # Inside the nested span, before any lodging bound to it would match —
    # the nested trip governs, and the Airbnb check-in (2025-07-06) is
    # within its span too, so lodging still wins.
    anchor = resolve_anchor(schedule, at=_at("2025-07-07T09:00:00Z"), home_address=HOME)
    assert anchor.address == AIRBNB


# ---------------------------------------------------------------------------
# resolve_effective_home (I/O convenience)
# ---------------------------------------------------------------------------


def test_effective_home_off_trip_is_static_home(tmp_path, monkeypatch):
    path = tmp_path / "travel-schedule.json"
    path.write_text(json.dumps(_uk_trip_schedule()))
    monkeypatch.setattr(trip_origin, "SCHEDULE_PATH", str(path))
    assert resolve_effective_home(HOME, now=_at("2025-07-20T12:00:00Z")) == HOME


def test_effective_home_on_trip_is_lodging(tmp_path, monkeypatch):
    path = tmp_path / "travel-schedule.json"
    path.write_text(json.dumps(_uk_trip_schedule()))
    monkeypatch.setattr(trip_origin, "SCHEDULE_PATH", str(path))
    assert resolve_effective_home(HOME, now=_at("2025-07-07T18:00:00Z")) == AIRBNB


def test_effective_home_missing_schedule_is_static_home(tmp_path, monkeypatch):
    monkeypatch.setattr(trip_origin, "SCHEDULE_PATH", str(tmp_path / "absent.json"))
    assert resolve_effective_home(HOME, now=_at("2025-07-07T18:00:00Z")) == HOME


def test_effective_home_mid_trip_unresolved_is_none_not_home(tmp_path, monkeypatch):
    schedule = _uk_trip_schedule()
    schedule[0]["location"] = None
    path = tmp_path / "travel-schedule.json"
    path.write_text(json.dumps(schedule))
    monkeypatch.setattr(trip_origin, "SCHEDULE_PATH", str(path))
    assert resolve_effective_home(HOME, now=_at("2025-06-28T12:00:00Z")) is None
