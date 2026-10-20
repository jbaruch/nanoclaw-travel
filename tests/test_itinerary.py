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

# E402: the module resolves only after the sys.path insert above.
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
    # byAir's 23:05 departure governs: TripIt's 23:00 copy opens no gap.
    assert itinerary.position_at(_at("2025-11-25T23:02:00Z")).source == "home"


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


def test_a_delayed_byair_copy_wins_over_the_schedules_booked_times():
    """byAir governs flight status: WN716 left three hours late, so an hour
    after its booked departure he is still home, not in the air."""
    delayed = [_transport("2025-11-26T02:00:00Z", "2025-11-26T04:25:00Z", "BNA", "SAT")]
    itinerary = Itinerary(THANKSGIVING, home_address=HOME, home_airports=BNA, transports=delayed)
    assert itinerary.position_at(_at("2025-11-26T00:00:00Z")).source == "home"
    assert itinerary.position_at(_at("2025-11-26T05:00:00Z")).address == AIRBNB


# --- review round 2 (#337) ----------------------------------------------------


WN716 = THANKSGIVING[2]  # BNA→SAT, booked 23:00Z


def _thanksgiving(*transports: Transport) -> Itinerary:
    return Itinerary(
        THANKSGIVING, home_address=HOME, home_airports=BNA, transports=list(transports)
    )


def test_two_connecting_trains_are_two_journeys():
    """Trains carry no endpoints; only an exact copy of one is a duplicate. The
    hotel checked into during the second train is reached when it arrives."""
    trains = [
        {
            "type": "Trip",
            "summary": "Rail",
            "start": "2025-10-16",
            "end": "2025-10-18",
            "location": "C",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hotel C",
            "start": "2025-10-16T12:30:00Z",
            "location": "1 C St",
        },
        {
            "type": "Rail",
            "summary": "A to B",
            "start": "2025-10-16T10:00:00Z",
            "end": "2025-10-16T11:00:00Z",
        },
        {
            "type": "Rail",
            "summary": "B to C",
            "start": "2025-10-16T12:00:00Z",
            "end": "2025-10-16T14:00:00Z",
        },
        {
            "type": "Rail",
            "summary": "B to C",
            "start": "2025-10-16T12:00:00Z",
            "end": "2025-10-16T14:00:00Z",
        },
    ]
    itinerary = Itinerary(trains, home_address=HOME)
    assert itinerary.position_at(_at("2025-10-16T13:00:00Z")).address != "1 C St"
    assert itinerary.position_at(_at("2025-10-16T15:00:00Z")).address == "1 C St"


def test_a_long_delay_still_replaces_the_booked_copy():
    """Seven hours late: byAir's copy carries the booked departure, so TripIt's
    23:00 copy does not move him away before the real one does."""
    late = Transport(
        _at("2025-11-26T06:00:00Z"),
        _at("2025-11-26T08:25:00Z"),
        "BNA",
        "SAT",
        scheduled=_at("2025-11-25T23:00:00Z"),
    )
    itinerary = _thanksgiving(late)
    assert itinerary.position_at(_at("2025-11-26T03:00:00Z")).source == "home"
    assert itinerary.position_at(_at("2025-11-26T09:00:00Z")).address == AIRBNB


def test_a_cancelled_flight_takes_the_booked_copy_with_it():
    cancelled = Transport(
        _at("2025-11-25T23:00:00Z"),
        _at("2025-11-26T01:25:00Z"),
        "BNA",
        "SAT",
        scheduled=_at("2025-11-25T23:00:00Z"),
        status="cancelled",
    )
    schedule = [THANKSGIVING[0], WN716]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=[cancelled])
    # Neither the cancelled flight nor TripIt's copy of it took him to SAT.
    assert itinerary.position_at(_at("2025-11-26T03:00:00Z")).source == "home"


def test_a_diverted_return_does_not_land_him_home():
    """WN3764 SAT→BNA diverted: the booked copy would say he landed at BNA."""
    diverted = Transport(
        _at("2025-11-29T23:45:00Z"),
        _at("2025-11-30T02:30:00Z"),
        "SAT",
        None,
        scheduled=_at("2025-11-29T23:45:00Z"),
        status="diverted",
    )
    position = _thanksgiving(diverted).position_at(_at("2025-11-30T03:00:00Z"))
    assert position.source != "home"


def test_a_check_out_during_the_flight_out_keeps_the_hotel_before_it():
    """Checked out at 11:00 while the 09:00–13:00 flight was in the air (the
    feed dropped the past check-in): the morning drive leaves the hotel."""
    schedule = [
        {
            "type": "Trip",
            "summary": "Rome",
            "start": "2025-10-14",
            "end": "2025-10-16",
            "location": "Rome",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Hotel Roma",
            "start": "2025-10-16T11:00:00Z",
            "location": "1 Via Roma",
        },
    ]
    flights = [_transport("2025-10-16T09:00:00Z", "2025-10-16T13:00:00Z", "FCO", "BNA")]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=flights)
    assert itinerary.position_at(_at("2025-10-16T06:30:00Z")).address == "1 Via Roma"


def test_a_check_in_during_the_flight_in_lands_with_him():
    """A 15:00 check-in while the 14:00–18:00 flight is in the air, with a
    connection on the way: after the final landing he is at the hotel."""
    schedule = [
        {
            "type": "Trip",
            "summary": "SF",
            "start": "2025-08-17",
            "end": "2025-08-20",
            "location": "San Francisco, CA",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hotel SF",
            "start": "2025-08-17T15:00:00Z",
            "location": "1 Market St",
        },
    ]
    flights = [
        _transport("2025-08-17T14:00:00Z", "2025-08-17T16:00:00Z", "BNA", "DEN"),
        _transport("2025-08-17T17:00:00Z", "2025-08-17T19:30:00Z", "DEN", "SFO"),
    ]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=flights)
    assert itinerary.position_at(_at("2025-08-17T21:00:00Z")).address == "1 Market St"
    assert itinerary.position_at(_at("2025-08-17T16:30:00Z")).address != "1 Market St"


TEL_AVIV = [
    {
        "type": "Trip",
        "summary": "Israel",
        "start": "2025-08-01",
        "end": "2025-08-20",
        "location": "Tel Aviv",
    },
    {
        "type": "Lodging",
        "summary": "Check-in: TLV Hotel",
        "start": "2025-08-02T13:00:00Z",
        "location": "Rothschild Blvd",
    },
    {
        "type": "Lodging",
        "summary": "Check-out: TLV Hotel",
        "start": "2025-08-20T08:00:00Z",
        "location": "Rothschild Blvd",
    },
]
TEL_AVIV_FLIGHTS = [
    _transport("2025-08-01T12:00:00Z", "2025-08-02T08:00:00Z", "BNA", "TLV"),
    _transport("2025-08-05T09:00:00Z", "2025-08-05T11:00:00Z", "TLV", "ATH"),
    _transport("2025-08-07T09:00:00Z", "2025-08-07T11:00:00Z", "ATH", "TLV"),
    _transport("2025-08-20T12:00:00Z", "2025-08-21T02:00:00Z", "TLV", "BNA"),
]


@pytest.mark.parametrize(
    ("at", "address"),
    [
        ("2025-08-03T09:00:00Z", "Rothschild Blvd"),  # before the side trip
        ("2025-08-05T06:00:00Z", "Rothschild Blvd"),  # the drive to TLV for ATH
        ("2025-08-06T09:00:00Z", "Tel Aviv"),  # in Athens: not the Tel Aviv hotel
        ("2025-08-10T09:00:00Z", "Rothschild Blvd"),  # back from Athens
    ],
)
def test_a_hotel_kept_through_a_side_trip_is_home_base_on_both_sides(at, address):
    itinerary = Itinerary(
        TEL_AVIV, home_address=HOME, home_airports=BNA, transports=TEL_AVIV_FLIGHTS
    )
    assert itinerary.position_at(_at(at)).address == address


def test_a_drive_trip_after_landing_home_anchors_at_its_hotel():
    """Landed at BNA, then drove to Gatlinburg: after checking out, while the
    trip is still on, the landing no longer says he is home."""
    schedule = [
        {
            "type": "Trip",
            "summary": "Gatlinburg",
            "start": "2025-07-11",
            "end": "2025-07-14",
            "location": "Gatlinburg, TN",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Fairfield Inn",
            "start": "2025-07-11T20:00:00Z",
            "location": "611 Historic Nature Trail",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Fairfield Inn",
            "start": "2025-07-13T15:00:00Z",
            "location": "611 Historic Nature Trail",
        },
    ]
    flights = [_transport("2025-07-10T14:00:00Z", "2025-07-10T16:00:00Z", "LGA", "BNA")]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=flights)
    assert itinerary.position_at(_at("2025-07-11T12:00:00Z")).source == "home"
    assert itinerary.position_at(_at("2025-07-13T17:00:00Z")).address == "611 Historic Nature Trail"


def test_a_repeat_visit_to_one_hotel_does_not_pair_across_trips():
    """A July check-in whose check-out the feed lost must not pair with the
    August visit's check-out and fill the weeks between with the hotel."""
    schedule = [
        {
            "type": "Trip",
            "summary": "July",
            "start": "2025-07-01",
            "end": "2025-07-03",
            "location": "Austin",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hotel A",
            "start": "2025-07-01T20:00:00Z",
            "location": "1 Main",
            "description": "https://www.tripit.com/trip/show/id/111",
        },
        {
            "type": "Trip",
            "summary": "August",
            "start": "2025-08-01",
            "end": "2025-08-03",
            "location": "Austin",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Hotel A",
            "start": "2025-08-01T20:00:00Z",
            "location": "1 Main",
            "description": "https://www.tripit.com/trip/show/id/222",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Hotel A",
            "start": "2025-08-03T15:00:00Z",
            "location": "1 Main",
            "description": "https://www.tripit.com/trip/show/id/222",
        },
    ]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA)
    assert itinerary.position_at(_at("2025-07-15T12:00:00Z")).source == "home"


def test_a_check_out_after_the_flight_home_stays_with_the_hotel_before_it():
    """SAT→BNA 12:00–14:00, the San Antonio check-out stamped 17:00: he is home
    after landing, and at the hotel before the flight."""
    schedule = [
        {
            "type": "Trip",
            "summary": "SA",
            "start": "2025-11-25",
            "end": "2025-11-30",
            "location": "San Antonio, TX",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Riverwalk Inn",
            "start": "2025-11-30T17:00:00Z",
            "location": "2 River Walk",
        },
    ]
    flights = [
        _transport("2025-11-25T12:00:00Z", "2025-11-25T14:00:00Z", "BNA", "SAT"),
        _transport("2025-11-30T12:00:00Z", "2025-11-30T14:00:00Z", "SAT", "BNA"),
    ]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=flights)
    assert itinerary.position_at(_at("2025-11-30T14:30:00Z")).source == "home"
    assert itinerary.position_at(_at("2025-11-30T09:00:00Z")).address == "2 River Walk"


def test_a_check_out_during_a_connection_stays_with_the_city_left():
    """FCO→CDG 09:00–11:00, check-out stamped 11:30, CDG→BNA 13:00: the Rome
    hotel is not in Paris."""
    schedule = [
        {
            "type": "Trip",
            "summary": "Rome",
            "start": "2025-10-12",
            "end": "2025-10-16",
            "location": "Rome",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Hotel Roma",
            "start": "2025-10-16T11:30:00Z",
            "location": "1 Via Roma",
        },
    ]
    flights = [
        _transport("2025-10-16T09:00:00Z", "2025-10-16T11:00:00Z", "FCO", "CDG"),
        _transport("2025-10-16T13:00:00Z", "2025-10-16T21:00:00Z", "CDG", "BNA"),
    ]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=flights)
    # In Paris he is not at the Rome hotel; it is only the last one he left.
    assert itinerary.position_at(_at("2025-10-16T11:15:00Z")).detail == "after Hotel Roma"
    assert itinerary.position_at(_at("2025-10-16T07:00:00Z")).detail == "Hotel Roma"


def test_a_delay_never_moves_a_hotel_across_its_flight():
    """Check-in-only Airbnb at 21:00, BNA→SAT booked 23:00 but delayed to
    06:00: the booking says the flight reaches the Airbnb, so at 22:00 he is
    still home."""
    schedule = [THANKSGIVING[0], {**THANKSGIVING[1], "end": None}]
    delayed = Transport(
        _at("2025-11-26T06:00:00Z"),
        _at("2025-11-26T08:25:00Z"),
        "BNA",
        "SAT",
        scheduled=_at("2025-11-25T23:00:00Z"),
    )
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=[delayed])
    assert itinerary.position_at(_at("2025-11-25T22:00:00Z")).source == "home"
    assert itinerary.position_at(_at("2025-11-26T09:00:00Z")).address == AIRBNB


def test_a_known_away_landing_stays_away_after_a_check_out():
    """#337 review: landed at SFO, stayed, checked out; no trip wrapper covers
    the day. The landing still says away, never home."""
    schedule = [
        {
            "type": "Lodging",
            "summary": "Union Square Inn",
            "start": "2025-08-17T20:00:00Z",
            "end": "2025-08-19T15:00:00Z",
            "location": "1 Geary St",
        },
    ]
    flights = [_transport("2025-08-17T14:00:00Z", "2025-08-17T19:00:00Z", "BNA", "SFO")]
    itinerary = Itinerary(schedule, home_address=HOME, home_airports=BNA, transports=flights)
    assert itinerary.position_at(_at("2025-08-19T17:00:00Z")).source != "home"
