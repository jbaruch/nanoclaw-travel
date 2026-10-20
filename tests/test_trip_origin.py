"""Tests for skills/travel-core/trip_origin.py (#122).

Locks the anchor-resolution contract:

  - `load_travel_schedule` is a tolerant non-owner reader: missing /
    corrupt / non-UTF-8 / non-list-root / forward-incompatible files all
    resolve to None (static-home behavior), never an exception
  - `resolve_anchor` is the itinerary's position (`itinerary.py`): a covering
    stay → that lodging; no trip, a trip not yet begun, or a period at a home
    airport → home; otherwise the lodging last checked out of on the trip, the
    trip's location, or unresolved (address None)
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
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))

import trip_origin  # noqa: E402
from trip_origin import (  # noqa: E402
    SCHEDULE_SCHEMA_VERSION,
    TripAnchor,
    flight_summaries,
    flight_windows,
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


def test_departure_day_before_first_flight_resolves_home():
    """On the departure day but before the trip's first flight lifts off the
    operator is still home — the date-only Trip wrapper is already 'active', but
    anchoring the outbound airport-departure drive at the destination is what
    drew the 34-hour cross-country block. The BNA→LHR flight departs
    2025-06-26T19:00Z; at 02:00Z that morning home wins."""
    anchor = resolve_anchor(_uk_trip_schedule(), at=_at("2025-06-26T02:00:00Z"), home_address=HOME)
    assert anchor.address == HOME
    assert anchor.source == "home"


def test_return_end_boundary_day_is_still_on_trip():
    """The trip-end boundary date stays on-trip (last lodging wins) — the safe
    direction for the #122 failure mode is unchanged after the first flight has
    departed."""
    anchor = resolve_anchor(_uk_trip_schedule(), at=_at("2025-07-13T22:00:00Z"), home_address=HOME)
    assert anchor.source != "home"


def test_outbound_departure_anchors_home_not_destination():
    """Regression for the live San Francisco→BNA block: a BNA→SFO trip whose
    outbound airport-departure drive resolved its origin at the trip's
    destination (SFO's city) instead of home, drawing a ~34-hour cross-country
    'drive'. The departure drive's origin instant is before the outbound flight,
    so it must anchor at home."""
    schedule = [
        _record(
            type="Trip",
            summary="San Francisco 2025",
            start="2025-08-16",
            end="2025-08-20",
            location="San Francisco, CA",
        ),
        _record(
            type="Flight",
            summary="BNA to SFO",
            start="2025-08-17T10:20:00Z",
            end="2025-08-17T15:00:00Z",
            location="Nashville International Airport",
        ),
    ]
    # The outbound airport-departure drive leaves ~an hour before wheels-up.
    anchor = resolve_anchor(schedule, at=_at("2025-08-17T09:15:00Z"), home_address=HOME)
    assert anchor.address == HOME
    assert anchor.source == "home"


def test_date_only_flight_is_not_read_as_a_midnight_departure():
    """A date-only `Flight` start (`YYYY-MM-DD`) carries no departure time. Read
    as midnight it would mark the trip as begun at 00:00Z of the flight day, so
    an instant that morning would read as away. Timed-only ignores it: nothing
    timed is booked, the trip has not begun, and he is home (#335)."""
    schedule = [
        _record(
            type="Trip",
            summary="San Francisco 2025",
            start="2025-08-16",
            end="2025-08-20",
            location="San Francisco, CA",
        ),
        _record(
            type="Flight",
            summary="BNA to SFO",
            start="2025-08-17",
            end="2025-08-17",
            location="Nashville International Airport",
        ),
    ]
    anchor = resolve_anchor(schedule, at=_at("2025-08-17T10:00:00Z"), home_address=HOME)
    assert anchor.address == HOME
    assert anchor.source == "home"


def test_a_trip_with_nothing_booked_is_home():
    """No transport and no lodging: nothing says he left. The old ladder read
    the trip's city here, which is how a local placeholder drew drives from a
    metro centroid (#314) and a far-off one from a city he never reached."""
    schedule = [
        _record(
            type="Trip",
            summary="Road trip 2025",
            start="2025-08-16",
            end="2025-08-20",
            location="Asheville, NC",
        ),
    ]
    anchor = resolve_anchor(schedule, at=_at("2025-08-16T02:00:00Z"), home_address=HOME)
    assert anchor.address == HOME
    assert anchor.source == "home"


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


# ---------------------------------------------------------------------------
# flight_windows — flight spans for drive-planner's scan filter (#85)
# ---------------------------------------------------------------------------


def test_flight_windows_none_or_empty_schedule_is_empty():
    assert flight_windows(None) == []
    assert flight_windows([]) == []


def test_flight_windows_extracts_flight_segments_only():
    # The UK fixture has one Flight (BNA→LHR) plus a Trip and Lodging records.
    windows = flight_windows(_uk_trip_schedule())
    assert windows == [
        (_at("2025-06-26T19:00:00Z"), _at("2025-06-27T07:30:00Z")),
    ]


def test_flight_windows_skips_date_only_flight():
    # A date-only "flight" would span whole calendar days and could suppress a
    # real same-day meeting — no window is emitted for it (the safe direction).
    schedule = [
        _record(
            type="Flight",
            summary="Date-only flight",
            start="2025-06-26",
            end="2025-06-27",
            location="Somewhere",
        ),
    ]
    assert flight_windows(schedule) == []


def test_flight_windows_skips_non_positive_span():
    schedule = [
        _record(
            type="Flight",
            summary="Zero-length flight",
            start="2025-06-26T19:00:00Z",
            end="2025-06-26T19:00:00Z",
            location="Somewhere",
        ),
    ]
    assert flight_windows(schedule) == []


# ---------------------------------------------------------------------------
# flight_summaries — flight identities for scan's code match (#85)
# ---------------------------------------------------------------------------


def test_flight_summaries_none_or_empty_schedule_is_empty():
    assert flight_summaries(None) == []
    assert flight_summaries([]) == []


def test_flight_summaries_returns_flight_segment_summaries_only():
    # The UK fixture has one Flight (BNA to LHR); Trip and Lodging are excluded.
    assert flight_summaries(_uk_trip_schedule()) == ["BNA to LHR"]


def test_flight_summaries_skips_blank_summary():
    schedule = [
        _record(
            type="Flight",
            summary="",
            start="2025-06-26T19:00:00Z",
            end="2025-06-26T22:00:00Z",
            location="x",
        ),
        _record(
            type="Flight",
            summary="DL 4908 BNA to LHR",
            start="2025-06-27T19:00:00Z",
            end="2025-06-27T22:00:00Z",
            location="y",
        ),
    ]
    assert flight_summaries(schedule) == ["DL 4908 BNA to LHR"]


def test_flight_summaries_includes_date_only_flight_segments():
    # Unlike flight_windows (which needs a timed span), the summary match is
    # time-agnostic — a date-only flight still contributes its identity.
    schedule = [
        _record(
            type="Flight",
            summary="DL 4908 BNA to LHR",
            start="2025-06-26",
            end="2025-06-27",
            location="x",
        ),
    ]
    assert flight_summaries(schedule) == ["DL 4908 BNA to LHR"]


# ---------------------------------------------------------------------------
# When the trip begins — transport departure, check-in only as a fallback (#233)
# ---------------------------------------------------------------------------


def _flightless_trip_schedule() -> list[dict]:
    """A drive-to-lodging trip: hotel booked, no transport segment at all."""
    return [
        _record(
            type="Trip",
            summary="TN Tigers 2025",
            start="2025-08-14",
            end="2025-08-16",
            location="Gatlinburg, TN",
        ),
        _record(
            type="Lodging",
            summary="Check-in: Fairfield Inn",
            start="2025-08-14T20:00:00Z",
            end="2025-08-14T21:00:00Z",
            location="611 Historic Nature Trail Gatlinburg TN",
        ),
    ]


def test_flightless_trip_before_checkin_resolves_home():
    """The gate used to key on a first FLIGHT, which a drive trip has none of,
    so it never fired and a first-day anchor fell through to the Trip wrapper's
    own location — the destination city. That is the cross-country-drive shape
    the gate exists to prevent, reached by the other door (#233)."""
    anchor = resolve_anchor(
        _flightless_trip_schedule(), at=_at("2025-08-14T12:00:00Z"), home_address=HOME
    )
    assert anchor.address == HOME
    assert anchor.source == "home"


def test_flightless_trip_after_checkin_resolves_to_the_lodging():
    """The other side of the gate: once checked in, the hotel wins. Gating the
    trip must not strand the operator at home for its whole duration."""
    anchor = resolve_anchor(
        _flightless_trip_schedule(), at=_at("2025-08-14T23:00:00Z"), home_address=HOME
    )
    assert anchor.source == "lodging"
    assert "Gatlinburg" in (anchor.address or "")


def _airport_hotel_schedule() -> list[dict]:
    """The #154 shape: an airport hotel the night before an early flight."""
    return [
        _record(
            type="Trip",
            summary="London 2025",
            start="2025-09-01",
            end="2025-09-05",
            location="London, UK",
        ),
        _record(
            type="Lodging",
            summary="Check-in: Hyatt Place Nashville Airport",
            start="2025-09-01T22:00:00Z",
            end="2025-09-01T23:00:00Z",
            location="Hyatt Place Nashville Airport",
        ),
        _record(
            type="Flight",
            summary="BNA to LHR",
            start="2025-09-02T11:00:00Z",
            end="2025-09-02T20:00:00Z",
            location="Nashville International Airport",
        ),
    ]


def test_airport_hotel_the_night_before_wins_over_home_on_the_flight_morning():
    """He slept at the hotel, so the morning airport drive starts there.

    Keying the gate on the first TRANSPORT departure read every instant before
    wheels-up as home, routing that drive from the house he had already left —
    the #154 shape reintroduced by the gate itself (#235). Earliest-of fixes it:
    the check-in came first, so the trip had already begun.

    The homecoming test that used to break when this changed now asks its own
    question (`opened_from_home`), so widening the gate no longer sends the
    drive home to this hotel.
    """
    anchor = resolve_anchor(
        _airport_hotel_schedule(), at=_at("2025-09-02T09:00:00Z"), home_address=HOME
    )
    assert anchor.source == "lodging"
    assert anchor.address == "Hyatt Place Nashville Airport"


def test_airport_hotel_trip_before_checkin_resolves_home():
    """The drive TO that hotel starts at home; the gate holds before check-in."""
    anchor = resolve_anchor(
        _airport_hotel_schedule(), at=_at("2025-09-01T12:00:00Z"), home_address=HOME
    )
    assert anchor.address == HOME
    assert anchor.source == "home"


def test_a_rail_departure_begins_a_trip_like_a_flight_does():
    """A train out is a departure from home too; before it the operator is home."""
    schedule = [
        _record(
            type="Trip",
            summary="Brussels 2025",
            start="2025-10-01",
            end="2025-10-03",
            location="Brussels, Belgium",
        ),
        _record(
            type="Rail",
            summary="Eurostar 9145",
            start="2025-10-01T14:00:00Z",
            end="2025-10-01T16:00:00Z",
            location="London St Pancras",
        ),
    ]
    before = resolve_anchor(schedule, at=_at("2025-10-01T09:00:00Z"), home_address=HOME)
    after = resolve_anchor(schedule, at=_at("2025-10-01T18:00:00Z"), home_address=HOME)
    assert before.source == "home"
    assert after.source == "trip_location"


def test_a_blank_location_checkin_does_not_begin_the_trip():
    """The lodging ladder cannot anchor on a blank location, so counting such a
    check-in as the trip's start would step past the gate and land on the
    destination city — the bug, not the fix."""
    schedule = _flightless_trip_schedule()
    schedule[1]["location"] = "   "
    anchor = resolve_anchor(schedule, at=_at("2025-08-14T23:00:00Z"), home_address=HOME)
    assert anchor.source == "trip_location"


def test_a_stay_known_only_by_its_checkout_runs_from_the_trips_start():
    """The refresh drops a stay's check-in once the stay is under way (#41), so
    a lone check-out is a stay in progress: it covers the trip up to check-out."""
    schedule = [
        _record(
            type="Trip",
            summary="TN Tigers 2025",
            start="2025-08-14",
            end="2025-08-16",
            location="Gatlinburg, TN",
        ),
        _record(
            type="Lodging",
            summary="Check-out: Fairfield Inn",
            start="2025-08-16T15:00:00Z",
            end="2025-08-16T16:00:00Z",
            location="611 Historic Nature Trail Gatlinburg TN",
        ),
    ]
    during = resolve_anchor(schedule, at=_at("2025-08-14T12:00:00Z"), home_address=HOME)
    assert during.address == "611 Historic Nature Trail Gatlinburg TN"


def test_a_date_only_checkin_does_not_gate_the_trip_to_home():
    """A date-only check-in carries no arrival time, so it cannot say when the
    operator left home and never gates. The lodging ladder's own reading of it
    (midnight, so the hotel wins from the start of that day) is unchanged — the
    gate is what must not act on an invented instant, not the ladder."""
    schedule = _flightless_trip_schedule()
    schedule[1]["start"] = "2025-08-14"
    schedule[1]["end"] = "2025-08-14"
    anchor = resolve_anchor(schedule, at=_at("2025-08-14T12:00:00Z"), home_address=HOME)
    assert anchor.source == "lodging"


# ---------------------------------------------------------------------------
# Coming home — the position after a landing (#235, #335)
# ---------------------------------------------------------------------------

BNA = frozenset({"BNA"})


def _round_trip_schedule() -> list[dict]:
    """BNA → JFK and back, with a hotel in New York."""
    return [
        _record(
            type="Trip",
            summary="New York 2025",
            start="2025-09-10",
            end="2025-09-13",
            location="New York, NY",
        ),
        _record(
            type="Flight",
            summary="DL100 BNA to JFK",
            start="2025-09-10T14:00:00Z",
            end="2025-09-10T16:30:00Z",
        ),
        _record(
            type="Lodging",
            summary="Check-in: Hotel Manhattan",
            start="2025-09-10T19:00:00Z",
            end="2025-09-10T20:00:00Z",
            location="1 Broadway, New York",
        ),
        _record(
            type="Lodging",
            summary="Check-out: Hotel Manhattan",
            start="2025-09-13T15:00:00Z",
            end="2025-09-13T16:00:00Z",
            location="1 Broadway, New York",
        ),
        _record(
            type="Flight",
            summary="DL101 JFK to BNA",
            start="2025-09-13T18:00:00Z",
            end="2025-09-13T20:30:00Z",
        ),
    ]


def test_landing_back_at_the_home_airport_is_home():
    """The trip's span still covers the return day; the home airport says he is
    back. The drive off that landing goes to the house."""
    anchor = resolve_anchor(
        _round_trip_schedule(),
        at=_at("2025-09-13T21:00:00Z"),
        home_address=HOME,
        home_airports=BNA,
    )
    assert (anchor.address, anchor.source) == (HOME, "home")


def test_between_checkout_and_the_flight_home_the_hotel_is_the_anchor():
    anchor = resolve_anchor(
        _round_trip_schedule(),
        at=_at("2025-09-13T16:30:00Z"),
        home_address=HOME,
        home_airports=BNA,
    )
    assert anchor.address == "1 Broadway, New York"


def test_a_journey_staged_at_an_airport_hotel_comes_home_to_the_house():
    """He slept at the airport hotel before flying out; the hotel belongs to
    the night before the flight, so the landing home goes to the house (#235)."""
    schedule = _airport_hotel_schedule() + [
        _record(
            type="Flight",
            summary="BA200 LHR to BNA",
            start="2025-09-05T15:00:00Z",
            end="2025-09-05T21:00:00Z",
        )
    ]
    anchor = resolve_anchor(
        schedule, at=_at("2025-09-05T22:00:00Z"), home_address=HOME, home_airports=BNA
    )
    assert (anchor.address, anchor.source) == (HOME, "home")


def _mid_trip_round_trip_schedule() -> list[dict]:
    """A long stay abroad with a round trip flown out of the destination."""
    return [
        _record(
            type="Trip",
            summary="Israel 2025",
            start="2025-08-01",
            end="2025-08-10",
            location="Tel Aviv, Israel",
        ),
        _record(
            type="Flight",
            summary="BNA to TLV",
            start="2025-08-01T12:00:00Z",
            end="2025-08-02T08:00:00Z",
        ),
        _record(
            type="Lodging",
            summary="Check-in: Hotel Tel Aviv",
            start="2025-08-02T13:00:00Z",
            end="2025-08-02T14:00:00Z",
            location="Rothschild Blvd, Tel Aviv",
        ),
        _record(
            type="Flight",
            summary="TLV to ATH",
            start="2025-08-05T09:00:00Z",
            end="2025-08-05T11:00:00Z",
        ),
        _record(
            type="Flight",
            summary="ATH to TLV",
            start="2025-08-07T09:00:00Z",
            end="2025-08-07T11:00:00Z",
        ),
    ]


def test_a_round_trip_flown_mid_stay_returns_to_the_stays_hotel():
    """Landing back in Tel Aviv is not landing home: the drive goes to the
    hotel, not to Tennessee."""
    anchor = resolve_anchor(
        _mid_trip_round_trip_schedule(),
        at=_at("2025-08-07T12:00:00Z"),
        home_address=HOME,
        home_airports=BNA,
    )
    assert anchor.address == "Rothschild Blvd, Tel Aviv"


def test_the_outbound_of_that_same_trip_leaves_from_home():
    anchor = resolve_anchor(
        _mid_trip_round_trip_schedule(),
        at=_at("2025-08-01T10:00:00Z"),
        home_address=HOME,
        home_airports=BNA,
    )
    assert (anchor.address, anchor.source) == (HOME, "home")


def test_no_home_address_configured_is_home_with_no_address():
    anchor = resolve_anchor(
        _round_trip_schedule(),
        at=_at("2025-09-13T21:00:00Z"),
        home_address=None,
        home_airports=BNA,
    )
    assert (anchor.address, anchor.source) == (None, "home")


def _drove_there_then_local_round_trip_schedule() -> list[dict]:
    """Drove to Gatlinburg, checked in, then flew a local round trip days later."""
    return [
        _record(
            type="Trip",
            summary="Gatlinburg 2025",
            start="2025-07-10",
            end="2025-07-16",
            location="Gatlinburg, TN",
        ),
        _record(
            type="Lodging",
            summary="Check-in: Fairfield Inn",
            start="2025-07-10T20:00:00Z",
            end="2025-07-10T21:00:00Z",
            location="611 Historic Nature Trail Gatlinburg TN",
        ),
        _record(
            type="Flight",
            summary="TYS to ATL",
            start="2025-07-13T09:00:00Z",
            end="2025-07-13T10:00:00Z",
        ),
        _record(
            type="Flight",
            summary="ATL to TYS",
            start="2025-07-15T18:00:00Z",
            end="2025-07-15T19:00:00Z",
        ),
    ]


def test_a_trip_driven_to_then_flown_out_of_returns_to_the_hotel():
    """TYS is not home: the landing goes back to the Gatlinburg hotel."""
    anchor = resolve_anchor(
        _drove_there_then_local_round_trip_schedule(),
        at=_at("2025-07-15T19:30:00Z"),
        home_address=HOME,
        home_airports=BNA,
    )
    assert anchor.address == "611 Historic Nature Trail Gatlinburg TN"


def test_before_driving_there_he_is_home():
    anchor = resolve_anchor(
        _drove_there_then_local_round_trip_schedule(),
        at=_at("2025-07-10T12:00:00Z"),
        home_address=HOME,
        home_airports=BNA,
    )
    assert (anchor.address, anchor.source) == (HOME, "home")


# ---------------------------------------------------------------------------
# Placeholder trips — nothing booked is home (#314)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location", ["Nashville, TN", "Franklin, TN", "Asheville, NC"])
def test_a_placeholder_trip_without_lodging_is_home(location):
    """Wherever the placeholder points, nothing booked means nothing moved him."""
    schedule = [
        _record(
            type="Trip",
            summary="Local placeholder",
            start="2025-07-07",
            end="2025-07-07",
            location=location,
        )
    ]
    anchor = resolve_anchor(schedule, at=_at("2025-07-07T18:00:00Z"), home_address=HOME)
    assert (anchor.address, anchor.source) == (HOME, "home")


def test_a_local_trip_with_lodging_anchors_at_the_hotel():
    schedule = [
        _record(
            type="Trip",
            summary="Downtown stay",
            start="2025-07-07",
            end="2025-07-08",
            location="Nashville, TN",
        ),
        _record(
            type="Lodging",
            summary="Check-in: Downtown Hotel",
            start="2025-07-07T15:00:00Z",
            end="2025-07-07T15:00:00Z",
            location="42 Example Ave",
        ),
    ]
    anchor = resolve_anchor(schedule, at=_at("2025-07-07T18:00:00Z"), home_address=HOME)
    assert (anchor.address, anchor.source) == ("42 Example Ave", "lodging")


def test_a_placeholder_without_a_home_address_does_not_guess_a_centroid():
    schedule = [
        _record(
            type="Trip",
            summary="Local placeholder",
            start="2025-07-07",
            end="2025-07-07",
            location="Nashville, TN",
        )
    ]
    anchor = resolve_anchor(schedule, at=_at("2025-07-07T18:00:00Z"), home_address=None)
    assert (anchor.address, anchor.source) == (None, "home")


def test_before_a_trips_first_stay_he_is_home():
    """An address-less check-in tomorrow has not begun the trip today."""
    schedule = [
        _record(
            type="Trip",
            summary="Local stay",
            start="2025-07-07",
            end="2025-07-08",
            location="Nashville, TN",
        ),
        _record(
            type="Lodging",
            summary="Check-in: Hotel",
            start="2025-07-08T15:00:00Z",
            end="2025-07-08T15:00:00Z",
        ),
    ]
    anchor = resolve_anchor(schedule, at=_at("2025-07-07T18:00:00Z"), home_address=HOME)
    assert (anchor.address, anchor.source) == (HOME, "home")


def test_a_prior_trips_checkout_does_not_anchor_a_placeholder():
    schedule = [
        _record(
            type="Trip",
            summary="Local placeholder",
            start="2025-07-07",
            end="2025-07-07",
            location="Nashville, TN",
        ),
        _record(
            type="Lodging",
            summary="Check-out: Prior Hotel",
            start="2025-07-06T10:00:00Z",
            end="2025-07-06T10:00:00Z",
            location="42 Example Ave",
        ),
    ]
    anchor = resolve_anchor(schedule, at=_at("2025-07-07T18:00:00Z"), home_address=HOME)
    assert (anchor.address, anchor.source) == (HOME, "home")


def test_effective_home_reads_the_profiles_home_airport(tmp_path, monkeypatch):
    """`resolve_effective_home` judges the return landing by the profile's
    `home_airport`: back at BNA is the house, not the New York hotel."""
    import addresses

    profile = tmp_path / "user_profile.md"
    profile.write_text("## Addresses\n- schema_version: 2\n- home_airport: BNA\n")
    schedule = tmp_path / "travel-schedule.json"
    schedule.write_text(json.dumps(_round_trip_schedule()))
    monkeypatch.setattr(addresses, "profile_path", lambda: profile)
    monkeypatch.setattr(trip_origin, "SCHEDULE_PATH", str(schedule))

    assert resolve_effective_home(HOME, now=_at("2025-09-13T21:00:00Z")) == HOME
    assert resolve_effective_home(HOME, now=_at("2025-09-11T12:00:00Z")) == "1 Broadway, New York"


# --- #323: a nominal check-in before the inbound flight --------------------


def _rome_to_amsterdam_schedule() -> list[dict]:
    """The live 'Fall Break + BaselOne' shape: Rome hotel until KL1608 flies
    FCO→AMS, but the Schiphol hotel's nominal check-in comes first."""
    return [
        {"type": "Trip", "summary": "Fall Break", "start": "2025-10-14", "end": "2025-10-20"},
        {
            "type": "Lodging",
            "summary": "Check-in: Dharma Boutique Hotel",
            "start": "2025-10-15T12:00:00Z",
            "location": "Via de' Ciancaleoni 49, Rome",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Dharma Boutique Hotel",
            "start": "2025-10-16T09:00:00Z",
            "location": "Via de' Ciancaleoni 49, Rome",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hilton Amsterdam Airport Schiphol",
            "start": "2025-10-16T13:00:00Z",
            "location": "Schiphol Boulevard 701, Schiphol",
        },
        {
            "type": "Flight",
            "summary": "KL1608 FCO to AMS",
            "start": "2025-10-16T17:45:00Z",
            "end": "2025-10-16T20:05:00Z",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Hilton Amsterdam Airport Schiphol",
            "start": "2025-10-17T08:00:00Z",
            "location": "Schiphol Boulevard 701, Schiphol",
        },
    ]


def test_the_airport_departure_origin_is_the_city_the_operator_is_still_in():
    """#323: the anchor before KL1608 is the Rome hotel, not the Schiphol one he
    only reaches on that flight."""
    anchor = resolve_anchor(
        _rome_to_amsterdam_schedule(),
        at=datetime.fromisoformat("2025-10-16T16:45:00+00:00"),
        home_address="home",
    )
    assert anchor.address == "Via de' Ciancaleoni 49, Rome"


def test_the_new_lodging_takes_over_once_the_inbound_flight_lands():
    anchor = resolve_anchor(
        _rome_to_amsterdam_schedule(),
        at=datetime.fromisoformat("2025-10-16T21:00:00+00:00"),
        home_address="home",
    )
    assert anchor.address == "Schiphol Boulevard 701, Schiphol"


def test_a_mid_stay_flight_days_later_does_not_move_the_check_in():
    schedule = _rome_to_amsterdam_schedule()
    schedule[4] = {
        "type": "Flight",
        "summary": "Day trip",
        "start": "2025-10-18T07:00:00Z",
        "end": "2025-10-18T09:00:00Z",
    }
    schedule[5] = {**schedule[5], "start": "2025-10-19T08:00:00Z"}
    anchor = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T16:45:00+00:00"), home_address="home"
    )
    assert anchor.address == "Schiphol Boulevard 701, Schiphol"


def test_a_previous_trips_hotel_is_not_a_hotel_change():
    """#331 review: the first lodging of a trip reached from home keeps its
    nominal check-in even when an earlier trip's hotel is still in the feed."""
    schedule = [
        {"type": "Trip", "summary": "Earlier", "start": "2026-09-01", "end": "2026-09-03"},
        {
            "type": "Lodging",
            "summary": "Check-in: Old Hotel",
            "start": "2026-09-01T15:00:00Z",
            "location": "1 Old St, Elsewhere",
        },
        {"type": "Trip", "summary": "Now", "start": "2026-10-10", "end": "2026-10-12"},
        {
            "type": "Lodging",
            "summary": "Check-in: Airport Hotel",
            "start": "2026-10-10T22:00:00Z",
            "location": "Airport Hotel, Nashville",
        },
        {
            "type": "Flight",
            "summary": "BNA to SFO",
            "start": "2026-10-11T07:00:00Z",
            "end": "2026-10-11T12:00:00Z",
        },
    ]
    anchor = resolve_anchor(
        schedule, at=datetime.fromisoformat("2026-10-11T05:00:00+00:00"), home_address="home"
    )
    assert anchor.address == "Airport Hotel, Nashville"


def _with_transports(*transports: dict) -> list[dict]:
    schedule = [record for record in _rome_to_amsterdam_schedule() if record["type"] != "Flight"]
    return schedule + list(transports)


@pytest.mark.parametrize(
    "legs",
    [
        pytest.param(
            (
                {
                    "type": "Rail",
                    "summary": "Roma Termini to Fiumicino Aeroporto",
                    "start": "2025-10-16T15:00:00Z",
                    "end": "2025-10-16T15:45:00Z",
                },
                {
                    "type": "Flight",
                    "summary": "KL1608 FCO to AMS",
                    "start": "2025-10-16T17:45:00Z",
                    "end": "2025-10-16T20:05:00Z",
                },
            ),
            id="rail-then-flight",
        ),
        pytest.param(
            (
                {
                    "type": "Flight",
                    "summary": "AF1005 FCO to CDG",
                    "start": "2025-10-16T14:00:00Z",
                    "end": "2025-10-16T16:10:00Z",
                },
                {
                    "type": "Flight",
                    "summary": "AF1340 CDG to AMS",
                    "start": "2025-10-16T18:45:00Z",
                    "end": "2025-10-16T20:05:00Z",
                },
            ),
            id="connecting-flights",
        ),
    ],
)
def test_the_new_lodging_waits_for_the_journeys_final_arrival(legs):
    """#331 review: between legs the operator has not reached the new city, so
    the previous lodging stays the anchor until the last leg lands."""
    schedule = _with_transports(*legs)

    between_legs = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T17:00:00+00:00"), home_address="home"
    )
    landed = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T21:00:00+00:00"), home_address="home"
    )

    assert between_legs.address == "Via de' Ciancaleoni 49, Rome"
    assert landed.address == "Schiphol Boulevard 701, Schiphol"


def test_a_staging_night_before_an_outbound_flight_keeps_its_check_in():
    """#331 review: after another lodging, an airport-hotel night followed by a
    morning flight out is outbound from that hotel, not the journey into it."""
    schedule = [
        {"type": "Trip", "summary": "Swing", "start": "2025-10-14", "end": "2025-10-20"},
        {
            "type": "Lodging",
            "summary": "Check-in: Dharma Boutique Hotel",
            "start": "2025-10-14T12:00:00Z",
            "location": "Via de' Ciancaleoni 49, Rome",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Dharma Boutique Hotel",
            "start": "2025-10-15T09:00:00Z",
            "location": "Via de' Ciancaleoni 49, Rome",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hilton Rome Airport",
            "start": "2025-10-15T22:00:00Z",
            "location": "Via Arturo Ferrarin 2, Fiumicino",
        },
        {
            "type": "Flight",
            "summary": "AZ608 FCO to JFK",
            "start": "2025-10-16T07:00:00Z",
            "end": "2025-10-16T09:00:00Z",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Hilton Rome Airport",
            "start": "2025-10-16T11:00:00Z",
            "location": "Via Arturo Ferrarin 2, Fiumicino",
        },
    ]

    anchor = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T05:00:00+00:00"), home_address="home"
    )

    assert anchor.address == "Via Arturo Ferrarin 2, Fiumicino"


def test_a_next_morning_departure_does_not_extend_the_inbound_journey():
    schedule = _rome_to_amsterdam_schedule() + [
        {
            "type": "Flight",
            "summary": "KL1981 AMS to BSL",
            "start": "2025-10-17T06:00:00Z",
            "end": "2025-10-17T07:20:00Z",
        }
    ]

    anchor = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T21:00:00+00:00"), home_address="home"
    )

    assert anchor.address == "Schiphol Boulevard 701, Schiphol"


def test_a_leg_spanning_the_nominal_check_in_still_defers_it():
    """#331 review: departing Rome before the 13:00Z nominal check-in and landing
    after it leaves the operator in transit at check-in time."""
    schedule = _with_transports(
        {
            "type": "Flight",
            "summary": "KL1606 FCO to AMS",
            "start": "2025-10-16T12:00:00Z",
            "end": "2025-10-16T15:00:00Z",
        }
    )

    in_transit = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T14:00:00+00:00"), home_address="home"
    )
    landed = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T16:00:00+00:00"), home_address="home"
    )

    assert in_transit.address == "Via de' Ciancaleoni 49, Rome"
    assert landed.address == "Schiphol Boulevard 701, Schiphol"


def test_a_flight_leaving_before_the_previous_checkout_is_still_inbound():
    """#331 review: departing 09:00 against an 11:00 nominal check-out and landing
    at 17:00 after a 15:00 nominal check-in keeps the operator in transit."""
    schedule = [
        {"type": "Trip", "summary": "Fall Break", "start": "2025-10-14", "end": "2025-10-20"},
        {
            "type": "Lodging",
            "summary": "Check-in: Dharma Boutique Hotel",
            "start": "2025-10-15T12:00:00Z",
            "location": "Via de' Ciancaleoni 49, Rome",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Dharma Boutique Hotel",
            "start": "2025-10-16T11:00:00Z",
            "location": "Via de' Ciancaleoni 49, Rome",
        },
        {
            "type": "Flight",
            "summary": "AZ110 FCO to AMS via LHR",
            "start": "2025-10-16T09:00:00Z",
            "end": "2025-10-16T17:00:00Z",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hilton Amsterdam Airport Schiphol",
            "start": "2025-10-16T15:00:00Z",
            "location": "Schiphol Boulevard 701, Schiphol",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Hilton Amsterdam Airport Schiphol",
            "start": "2025-10-17T08:00:00Z",
            "location": "Schiphol Boulevard 701, Schiphol",
        },
    ]

    airborne = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T16:00:00+00:00"), home_address="home"
    )
    landed = resolve_anchor(
        schedule, at=datetime.fromisoformat("2025-10-16T18:00:00+00:00"), home_address="home"
    )

    assert airborne.address == "Via de' Ciancaleoni 49, Rome"
    assert landed.address == "Schiphol Boulevard 701, Schiphol"
