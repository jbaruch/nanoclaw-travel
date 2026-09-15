"""Tests for skills/travel-core/trip_origin.py (#122).

Locks the anchor-resolution contract:

  - `load_travel_schedule` is a tolerant non-owner reader: missing /
    corrupt / non-UTF-8 / non-list-root / forward-incompatible files all
    resolve to None (static-home behavior), never an exception
  - `resolve_anchor` rules: off-trip → home; on-trip → latest Lodging
    event (check-in OR check-out) within the trip span at or before the
    anchor time; pre-first-lodging → the Trip's own location; nothing →
    unresolved (address None). Home-metro trips without lodging use home
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
    opened_from_home,
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
    as midnight it would falsely mark the trip as already departed — so an anchor
    the day BEFORE the flight date (00:00Z of the flight day > the anchor) would
    spuriously flip to home. Timed-only (mirroring flight_windows) ignores the
    date-only flight, leaving the trip with no known departure, so the pre-lodging
    anchor stays the trip location as the flightless contract prescribes. The
    query sits before the would-be midnight so it discriminates the fix from the
    midnight-parse bug."""
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
    anchor = resolve_anchor(schedule, at=_at("2025-08-16T10:00:00Z"), home_address=HOME)
    assert anchor.address == "San Francisco, CA"
    assert anchor.source == "trip_location"


def test_flightless_trip_keeps_pre_departure_trip_location():
    """A trip with no timed flight in the feed has nothing marking when the
    operator left home, so the pre-lodging anchor stays the trip location — the
    gate only fires when a first-flight departure is known."""
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
    assert anchor.address == "Asheville, NC"
    assert anchor.source == "trip_location"


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


def test_a_checkout_record_does_not_begin_the_trip():
    """Both sides of a stay are `Lodging`; only the check-in marks arrival."""
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
    # With only a check-out there is no begin instant, so the gate cannot fire
    # and the pre-existing flightless contract (trip location) stands.
    anchor = resolve_anchor(schedule, at=_at("2025-08-14T12:00:00Z"), home_address=HOME)
    assert anchor.source == "trip_location"


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
# opened_from_home — "did the JOURNEY leave home", not "is he in the house"
# ---------------------------------------------------------------------------


def _mid_trip_round_trip_schedule() -> list[dict]:
    """A long stay abroad with a round trip flown out of the destination.

    The Athens hop returns to the Tel Aviv hotel, not across an ocean — the case
    that keeps `opened_from_home` from simply answering yes to every round trip.
    """
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
            location="Nashville International Airport",
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
            location="Ben Gurion Airport",
        ),
    ]


def test_a_journey_leaving_the_house_opened_from_home():
    assert opened_from_home(_uk_trip_schedule(), at=_at("2025-06-26T17:00:00Z"), home_address=HOME)


def test_a_journey_staged_at_an_airport_hotel_still_opened_from_home():
    """The whole point: he is at a hotel, but he got there FROM the house, so
    the round trip still closes at the house. The old proxy said no here and
    routed the drive home to the hotel (#235)."""
    assert opened_from_home(
        _airport_hotel_schedule(), at=_at("2025-09-02T09:00:00Z"), home_address=HOME
    )


def test_a_round_trip_flown_mid_stay_did_not_open_from_home():
    """Its departure is not the trip's first, so it returns to the Tel Aviv
    hotel. Answering yes here would drive the operator to Tennessee."""
    assert not opened_from_home(
        _mid_trip_round_trip_schedule(), at=_at("2025-08-05T07:00:00Z"), home_address=HOME
    )


def test_the_outbound_of_that_same_trip_did_open_from_home():
    """The discriminator is which departure, not which trip — the BNA leg of the
    very schedule above still opens at home."""
    assert opened_from_home(
        _mid_trip_round_trip_schedule(), at=_at("2025-08-01T10:00:00Z"), home_address=HOME
    )


def test_no_home_address_configured_is_never_a_homecoming():
    """There is nothing to route the drive home to."""
    assert not opened_from_home(
        _uk_trip_schedule(), at=_at("2025-06-26T17:00:00Z"), home_address=None
    )


def test_an_off_trip_departure_opened_from_home():
    """A flight TripIt never ingested has no Trip wrapper; the planned position
    is the house, which is answer enough."""
    assert opened_from_home([], at=_at("2025-06-26T17:00:00Z"), home_address=HOME)


def _drove_there_then_local_round_trip_schedule() -> list[dict]:
    """Drove to Gatlinburg, checked in, then flies a local round trip days later.

    Its first transport departure IS that round trip, so "is this the trip's
    first departure" alone would call it home-originating and route the drive
    off the final landing to Tennessee. The check-in's lead time is what says
    otherwise: he has been living at the destination for days.
    """
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
            location="McGhee Tyson Airport",
        ),
        _record(
            type="Flight",
            summary="ATL to TYS",
            start="2025-07-15T18:00:00Z",
            end="2025-07-15T19:00:00Z",
            location="Hartsfield-Jackson",
        ),
    ]


def test_a_trip_driven_to_then_flown_out_of_did_not_open_from_home():
    """He drove there days earlier; the round trip returns to the hotel, not to
    Tennessee. Both this helper and the proxy it replaced answered yes here —
    the lead-time separator is what fixes it."""
    assert not opened_from_home(
        _drove_there_then_local_round_trip_schedule(),
        at=_at("2025-07-13T08:00:00Z"),
        home_address=HOME,
    )


def test_the_staging_separator_is_the_check_ins_lead_time():
    """Same schedule, check-in pulled to the night before the flight: now it is
    an overnight stay he drove to from the house, so the trip opens at home."""
    schedule = _drove_there_then_local_round_trip_schedule()
    schedule[1]["start"] = "2025-07-12T22:00:00Z"
    schedule[1]["end"] = "2025-07-12T23:00:00Z"
    assert opened_from_home(schedule, at=_at("2025-07-13T08:00:00Z"), home_address=HOME)


@pytest.mark.parametrize("location", ["Nashville, TN", "  NASHVILLE,   TN  "])
def test_home_metro_without_lodging_anchors_at_home(location):
    schedule = [
        _record(
            type="Trip",
            summary="Local placeholder",
            start="2025-07-07",
            end="2025-07-07",
            location=location,
        )
    ]
    anchor = resolve_anchor(
        schedule,
        at=_at("2025-07-07T18:00:00Z"),
        home_address=HOME,
        home_metros=frozenset({"nashville, tn"}),
    )
    assert anchor.address == HOME
    assert anchor.source == "home"


@pytest.mark.parametrize("metros", [frozenset(), frozenset({"franklin, tn"})])
def test_trip_without_matching_home_metro_keeps_trip_location(metros):
    schedule = [
        _record(
            type="Trip",
            summary="Trip",
            start="2025-07-07",
            end="2025-07-07",
            location="Nashville, TN",
        )
    ]
    anchor = resolve_anchor(
        schedule, at=_at("2025-07-07T18:00:00Z"), home_address=HOME, home_metros=metros
    )
    assert anchor.address == "Nashville, TN"
    assert anchor.source == "trip_location"


def test_home_metro_with_lodging_keeps_hotel_anchor():
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
    anchor = resolve_anchor(
        schedule,
        at=_at("2025-07-07T18:00:00Z"),
        home_address=HOME,
        home_metros=frozenset({"nashville, tn"}),
    )
    assert anchor.address == "42 Example Ave"
    assert anchor.source == "lodging"


def test_home_metro_without_home_address_does_not_guess_centroid():
    schedule = [
        _record(
            type="Trip",
            summary="Local placeholder",
            start="2025-07-07",
            end="2025-07-07",
            location="Nashville, TN",
        )
    ]
    anchor = resolve_anchor(
        schedule,
        at=_at("2025-07-07T18:00:00Z"),
        home_address=None,
        home_metros=frozenset({"nashville, tn"}),
    )
    assert anchor.address is None
    assert anchor.source == "home"


def test_home_metro_with_future_addressless_lodging_is_not_a_placeholder():
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
    anchor = resolve_anchor(
        schedule,
        at=_at("2025-07-07T18:00:00Z"),
        home_address=HOME,
        home_metros=frozenset({"nashville, tn"}),
    )
    assert anchor.address == "Nashville, TN"
    assert anchor.source == "trip_location"


def test_home_metro_ignores_lodging_from_prior_trip():
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
    anchor = resolve_anchor(
        schedule,
        at=_at("2025-07-07T18:00:00Z"),
        home_address=HOME,
        home_metros=frozenset({"nashville, tn"}),
    )
    assert anchor.address == HOME
    assert anchor.source == "home"


def test_effective_home_reads_profile_metro_for_local_placeholder(tmp_path, monkeypatch):
    import addresses

    profile = tmp_path / "user_profile.md"
    profile.write_text("## Addresses\n- schema_version: 2\n- home_metro: Nashville, TN\n")
    schedule = tmp_path / "travel-schedule.json"
    schedule.write_text(
        json.dumps(
            [
                _record(
                    type="Trip",
                    summary="Local placeholder",
                    start="2025-07-07",
                    end="2025-07-07",
                    location="Nashville, TN",
                )
            ]
        )
    )
    monkeypatch.setattr(addresses, "profile_path", lambda: profile)
    monkeypatch.setattr(trip_origin, "SCHEDULE_PATH", str(schedule))

    assert resolve_effective_home(HOME, now=_at("2025-07-07T18:00:00Z")) == HOME
