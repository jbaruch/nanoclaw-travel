"""flight-assist reads its "home" off the itinerary with byAir's flights (#335).

`precheck.byair_transports` turns the tracked flight records into itinerary
transports from this skill's own byAir poll: the snapshot's airport codes and
live times. A cancelled flight is left out, a diverted one lands at no known
airport, and a record never polled or unreadable is left out rather than
guessed.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

# E402: the skill modules resolve only after the sys.path insert above.
import precheck  # noqa: E402
from state import StateError  # noqa: E402


def _record(
    dep: str | None = "BNA",
    arr: str | None = "LGA",
    departs="2025-11-02T18:40:00Z",
    arrives="2025-11-02T21:05:00Z",
    **snapshot,
):
    return {
        "dep_airport_id": 98,
        "arr_airport_id": 3,
        "scheduled_dep_time": departs,
        "scheduled_arr_time": arrives,
        "last_snapshot": {
            "dep_airport_code": dep,
            "arr_airport_code": arr,
            "computed_status": "scheduled",
            **snapshot,
        },
    }


def _at(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC)


def test_tracked_flights_become_transports_by_their_polled_codes():
    records = {
        1: _record(),
        2: _record(
            dep="LGA", arr="BNA", departs="2025-11-04T22:00:00Z", arrives="2025-11-05T00:29:00Z"
        ),
    }
    transports = precheck.byair_transports([1, 2], read_state=records.get)
    assert [(t.origin, t.destination) for t in transports] == [("BNA", "LGA"), ("LGA", "BNA")]


def test_a_delayed_flight_moves_him_on_its_live_times():
    record = _record(dep_time="2025-11-02T21:40:00Z", arr_time="2025-11-03T00:05:00Z")
    (transport,) = precheck.byair_transports([1], read_state=lambda _id: record)
    assert (transport.departs, transport.arrives) == (
        _at("2025-11-02T21:40:00Z"),
        _at("2025-11-03T00:05:00Z"),
    )


def test_a_cancelled_flight_moves_nobody():
    record = _record(computed_status="cancelled")
    assert precheck.byair_transports([1], read_state=lambda _id: record) == []


def test_a_diverted_flight_lands_at_no_known_airport():
    record = _record(computed_status="diverted")
    (transport,) = precheck.byair_transports([1], read_state=lambda _id: record)
    assert (transport.origin, transport.destination) == ("BNA", None)


def test_a_flight_never_polled_is_left_out():
    record = {**_record(), "last_snapshot": None}
    assert precheck.byair_transports([1], read_state=lambda _id: record) == []


def test_a_snapshot_without_airport_codes_is_left_out():
    record = _record(dep=None)
    assert precheck.byair_transports([1], read_state=lambda _id: record) == []


def test_an_unreadable_or_untimed_record_is_left_out(capsys):
    def read(flight_id):
        if flight_id == 1:
            raise StateError("bad record")
        if flight_id == 2:
            raise IsADirectoryError("flight-2.json")
        if flight_id == 3:
            return None
        return _record(arrives="not a time")

    assert precheck.byair_transports([1, 2, 3, 4], read_state=read) == []
    assert capsys.readouterr().err.count("state unreadable") == 2
