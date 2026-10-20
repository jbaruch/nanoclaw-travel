"""Tests for skills/travel-core/itinerary.py — the one position model (#335).

The live #335 shapes, moved to 2025 (fixtures are fixed past dates per
`coding-policy: testing-standards`):

  - DevCon: a New York trip with no lodging in the schedule and its flights
    tracked by byAir alone. The schedule-only ladder never learned he had left,
    placed him in New York on the morning of the flight, and skipped the
    home → BNA drive as a 13.9-hour "drive".
  - Thanksgiving: an Airbnb whose nominal 15:00 check-in comes two hours before
    the 17:00 flight to San Antonio. The ladder put him at the Airbnb before he
    had left Nashville.

and the timeline's own contract: transports from byAir and TripIt collapse to
one journey, an instant in the air belongs to where he is going, and without
home airports a begun trip reads as away until its span ends.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))

from itinerary import Itinerary, Transport, schedule_transports  # noqa: E402

HOME = "12 Example St, Sampleton, TN 37000"
BNA = frozenset({"BNA"})
AIRBNB = "115 Devine St, San Antonio, TX 78210, USA"


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _transport(departs: str, arrives: str, origin: str, destination: str) -> Transport:
    return Transport(_at(departs), _at(arrives), origin, destination)


DEVCON = [
    {
        "type": "Trip",
        "summary": "DevCon Fall 2025",
        "start": "2025-11-02",
        "end": "2025-11-05",
        "location": "New York, NY",
    }
]
DEVCON_BYAIR = [
    _transport("2025-11-02T18:40:00Z", "2025-11-02T21:05:00Z", "BNA", "LGA"),
    _transport("2025-11-04T22:00:00Z", "2025-11-05T00:29:00Z", "LGA", "BNA"),
]


def _devcon(home_airports=BNA) -> Itinerary:
    return Itinerary(
        DEVCON, home_address=HOME, home_airports=home_airports, transports=DEVCON_BYAIR
    )


def test_the_morning_of_a_byair_only_flight_is_home():
    """#335: the BNA departure drive starts from the house, not from New York."""
    position = _devcon().position_at(_at("2025-11-02T16:40:00Z"))
    assert (position.address, position.source) == (HOME, "home")


def test_during_a_trip_with_no_lodging_he_is_in_the_trip_city():
    position = _devcon().position_at(_at("2025-11-03T15:00:00Z"))
    assert (position.address, position.source) == ("New York, NY", "trip_location")


def test_after_the_byair_only_flight_home_lands_he_is_home():
    """#335: the drive off the LGA→BNA landing goes to the house."""
    position = _devcon().position_at(_at("2025-11-05T01:00:00Z"))
    assert (position.address, position.source) == (HOME, "home")


def test_without_home_airports_a_begun_trip_is_away_until_its_span_ends():
    """The documented degraded mode: nothing says BNA is home."""
    position = _devcon(home_airports=frozenset()).position_at(_at("2025-11-05T01:00:00Z"))
    assert position.source == "trip_location"


THANKSGIVING = [
    {
        "type": "Trip",
        "summary": "Thanksgiving San Antonio 2025",
        "start": "2025-11-25",
        "end": "2025-11-30",
        "location": "San Antonio, TX",
    },
    {
        "type": "Lodging",
        "summary": "Check-in: Airbnb - Jennifer",
        "start": "2025-11-25T21:00:00Z",
        "end": "2025-11-25T22:00:00Z",
        "location": AIRBNB,
    },
    {
        "type": "Flight",
        "summary": "WN716 BNA to SAT",
        "start": "2025-11-25T23:00:00Z",
        "end": "2025-11-26T01:25:00Z",
    },
    {
        "type": "Lodging",
        "summary": "Check-out: Airbnb - Jennifer",
        "start": "2025-11-29T16:00:00Z",
        "end": "2025-11-29T17:00:00Z",
        "location": AIRBNB,
    },
    {
        "type": "Flight",
        "summary": "WN3764 SAT to BNA",
        "start": "2025-11-29T23:45:00Z",
        "end": "2025-11-30T01:55:00Z",
    },
]


@pytest.mark.parametrize(
    ("at", "address"),
    [
        ("2025-11-25T21:20:00Z", HOME),  # leave-by for WN716, after the nominal check-in
        ("2025-11-26T02:00:00Z", AIRBNB),  # landed in San Antonio
        ("2025-11-29T22:00:00Z", AIRBNB),  # checked out, leaving for SAT
        ("2025-11-30T02:30:00Z", HOME),  # landed back at BNA
    ],
)
def test_a_check_in_before_the_flight_takes_effect_when_it_lands(at, address):
    position = Itinerary(THANKSGIVING, home_address=HOME, home_airports=BNA).position_at(_at(at))
    assert position.address == address


def test_in_the_air_he_belongs_to_where_he_is_going():
    """Not home, and not yet at the Airbnb: the destination's city."""
    position = Itinerary(THANKSGIVING, home_address=HOME, home_airports=BNA).position_at(
        _at("2025-11-26T00:00:00Z")
    )
    assert (position.address, position.source) == ("San Antonio, TX", "trip_location")


def test_the_same_flight_from_two_sources_is_one_journey():
    """The feed triplicates segments and byAir tracks the same flight: one
    journey, so no phantom ground period opens between the copies."""
    repeated = THANKSGIVING + [THANKSGIVING[2], THANKSGIVING[2]]
    byair = [_transport("2025-11-25T23:05:00Z", "2025-11-26T01:30:00Z", "BNA", "SAT")]
    itinerary = Itinerary(repeated, home_address=HOME, home_airports=BNA, transports=byair)
    assert itinerary.position_at(_at("2025-11-26T02:00:00Z")).address == AIRBNB
    assert len(itinerary._transports) == 2  # outbound and return


def test_schedule_transports_reads_timed_flights_and_trains():
    transports = schedule_transports(
        [
            {
                "type": "Flight",
                "summary": "WN716 BNA to SAT",
                "start": "2025-11-25T23:00:00Z",
                "end": "2025-11-26T01:25:00Z",
            },
            {
                "type": "Rail",
                "summary": "Roma Termini to Fiumicino",
                "start": "2025-10-16T15:00:00Z",
                "end": "2025-10-16T15:45:00Z",
            },
            {"type": "Flight", "summary": "BNA to SFO", "start": "2025-08-17", "end": "2025-08-17"},
            {"type": "Lodging", "summary": "Check-in: X", "start": "2025-08-17T15:00:00Z"},
        ]
    )
    assert {(t.origin, t.destination) for t in transports} == {("BNA", "SAT"), (None, None)}


def test_no_schedule_and_no_transports_is_home():
    position = Itinerary(None, home_address=HOME).position_at(_at("2025-11-03T15:00:00Z"))
    assert (position.address, position.source) == (HOME, "home")


def test_away_with_nothing_to_anchor_is_unresolved():
    schedule = [
        {"type": "Trip", "summary": "Somewhere", "start": "2025-11-02", "end": "2025-11-05"}
    ]
    position = Itinerary(
        schedule, home_address=HOME, home_airports=BNA, transports=DEVCON_BYAIR
    ).position_at(_at("2025-11-03T15:00:00Z"))
    assert (position.address, position.source) == (None, "unresolved")
    assert position.detail is not None and "Somewhere" in position.detail


def test_a_naive_instant_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        Itinerary(None, home_address=HOME).position_at(datetime(2025, 11, 3, 15, 0))


def test_a_utc_instant_from_another_zone_reads_the_same():
    eastern = datetime(2025, 11, 2, 11, 40, tzinfo=UTC).astimezone()
    assert _devcon().position_at(eastern).source == "home"


def test_a_stale_trip_wrapper_does_not_keep_him_home():
    """The local schedule copy refreshes nightly and can lag TripIt re-snapping a
    trip to its flights: on 2026-10-19 it still dated KubeCon a day after the
    BNA→SLC flight. The landing at a non-home airport, not the wrapper's dates,
    says he is away."""
    schedule = [
        {
            "type": "Trip",
            "summary": "KubeCon 2025",
            "start": "2025-11-09",
            "end": "2025-11-13",
            "location": "Salt Lake City, UT",
        }
    ]
    flights = [
        _transport("2025-11-08T20:00:00Z", "2025-11-08T23:30:00Z", "BNA", "SLC"),
        _transport("2025-11-13T00:30:00Z", "2025-11-13T04:08:00Z", "SLC", "BNA"),
    ]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=flights)
    assert itinerary.position_at(_at("2025-11-08T19:00:00Z")).source == "home"
    assert itinerary.position_at(_at("2025-11-10T18:00:00Z")).address == "Salt Lake City, UT"
    assert itinerary.position_at(_at("2025-11-13T04:40:00Z")).source == "home"


def _check_in_only_trip(check_in: str) -> list[dict]:
    """San Antonio with a check-in the feed never paired with a check-out."""
    return [
        THANKSGIVING[0],
        {
            "type": "Lodging",
            "summary": "Check-in: Airbnb - Jennifer",
            "start": check_in,
            "location": AIRBNB,
        },
        THANKSGIVING[2],
        THANKSGIVING[4],
    ]


def test_a_check_in_only_stay_hours_before_its_flight_waits_for_the_landing():
    """The 15:00 check-in two hours before the 17:00 flight to it: before the
    flight he is still home, after it he is at the Airbnb."""
    itinerary = Itinerary(
        _check_in_only_trip("2025-11-25T21:00:00Z"), home_address=HOME, home_airports=BNA
    )
    assert itinerary.position_at(_at("2025-11-25T21:20:00Z")).source == "home"
    assert itinerary.position_at(_at("2025-11-26T02:00:00Z")).address == AIRBNB


def test_a_check_in_only_stay_slept_in_before_the_flight_is_the_hotel():
    """Checked in the evening before an early flight: that night he is there."""
    schedule = _check_in_only_trip("2025-11-25T02:00:00Z")
    position = Itinerary(schedule, home_address=HOME, home_airports=BNA).position_at(
        _at("2025-11-25T12:00:00Z")
    )
    assert position.address == AIRBNB


CHICAGO_TWICE = [
    {
        "type": "Trip",
        "summary": "Chicago twice 2025",
        "start": "2025-09-01",
        "end": "2025-09-10",
        "location": "Chicago, IL",
    },
    {
        "type": "Lodging",
        "summary": "Check-out: Loop Hotel",
        "start": "2025-09-03T15:00:00Z",
        "location": "1 Loop Pl, Chicago",
    },
]
CHICAGO_FLIGHTS = [
    _transport("2025-09-01T14:00:00Z", "2025-09-01T15:30:00Z", "BNA", "ORD"),
    _transport("2025-09-03T18:00:00Z", "2025-09-03T19:30:00Z", "ORD", "BNA"),
    _transport("2025-09-05T14:00:00Z", "2025-09-05T15:30:00Z", "BNA", "ORD"),
]


def _chicago() -> Itinerary:
    return Itinerary(
        CHICAGO_TWICE, home_address=HOME, home_airports=BNA, transports=CHICAGO_FLIGHTS
    )


def test_a_check_out_only_stay_begins_when_he_lands():
    """The stay belongs to the period he checks out of, so the morning of the
    flight out is still home."""
    itinerary = _chicago()
    assert itinerary.position_at(_at("2025-09-01T10:00:00Z")).source == "home"
    assert itinerary.position_at(_at("2025-09-02T12:00:00Z")).address == "1 Loop Pl, Chicago"


def test_a_hotel_left_before_a_spell_at_home_does_not_anchor_the_next_visit():
    """He flew home between two Chicago visits: the second, with nothing booked
    yet, is the trip's city, not the hotel of the first."""
    position = _chicago().position_at(_at("2025-09-06T12:00:00Z"))
    assert (position.address, position.source) == ("Chicago, IL", "trip_location")


def test_without_a_trip_an_earlier_hotel_does_not_anchor_the_next_city():
    """No trip wrapper ties Chicago to San Francisco: landing at SFO with nothing
    booked is unresolved, not the Chicago hotel."""
    schedule = [
        {
            "type": "Lodging",
            "summary": "Loop Hotel",
            "start": "2025-09-01T20:00:00Z",
            "end": "2025-09-03T15:00:00Z",
            "location": "1 Loop Pl, Chicago",
        }
    ]
    flights = [
        _transport("2025-09-01T14:00:00Z", "2025-09-01T15:30:00Z", "BNA", "ORD"),
        _transport("2025-09-03T18:00:00Z", "2025-09-03T22:30:00Z", "ORD", "SFO"),
    ]
    position = Itinerary(
        schedule, home_address=HOME, home_airports=BNA, transports=flights
    ).position_at(_at("2025-09-04T12:00:00Z"))
    assert (position.address, position.source) == (None, "unresolved")
