"""flight-assist reads its "home" off the itinerary with byAir's flights (#335).

`precheck.byair_transports` turns the tracked flight records into itinerary
transports: airport ids map to IATA codes through the drive engine's
airport-facts cache, and a record that cannot be read, or whose airports the
cache has not seen, is left out rather than guessed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

import precheck  # noqa: E402
from airport_facts_cache import StaticAirport  # noqa: E402
from state import StateError  # noqa: E402

FACTS = {98: StaticAirport(iata="BNA"), 3: StaticAirport(iata="LGA")}


def _record(dep=98, arr=3, departs="2025-11-02T18:40:00Z", arrives="2025-11-02T21:05:00Z"):
    return {
        "dep_airport_id": dep,
        "arr_airport_id": arr,
        "scheduled_dep_time": departs,
        "scheduled_arr_time": arrives,
    }


def test_tracked_flights_become_transports_by_iata():
    records = {
        1: _record(),
        2: _record(dep=3, arr=98, departs="2025-11-04T22:00:00Z", arrives="2025-11-05T00:29:00Z"),
    }
    transports = precheck.byair_transports([1, 2], read_state=records.get, load_facts=lambda: FACTS)
    assert [(t.origin, t.destination) for t in transports] == [("BNA", "LGA"), ("LGA", "BNA")]


def test_a_flight_the_cache_has_not_seen_is_left_out():
    transports = precheck.byair_transports(
        [1], read_state=lambda _id: _record(arr=777), load_facts=lambda: FACTS
    )
    assert transports == []


def test_an_unreadable_or_untimed_record_is_left_out(capsys):
    def read(flight_id):
        if flight_id == 1:
            raise StateError("bad record")
        if flight_id == 2:
            raise IsADirectoryError("flight-2.json")
        return _record(arrives="not a time")

    assert precheck.byair_transports([1, 2, 3], read_state=read, load_facts=lambda: FACTS) == []
    assert capsys.readouterr().err.count("state unreadable") == 2
