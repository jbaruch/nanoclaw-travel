"""Tests for flight-chain connection classification and leg planning.

Deterministic fixtures only — fixed tz-aware datetimes and hand-built MergedFlights,
no wall-clock. These pin the #156 §D taxonomy as amended by the owner-decided
C2 / W2: same-airport connections default to silence at any gap and emit a leg pair
only on positive "operator left" evidence; different-airport pairs always transfer;
a lodging check-in between segments always decomposes into an overnight; and the
chain's opening departure + closing arrival always exist.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from chain import (  # noqa: E402
    ConnectionKind,
    LegKind,
    PairContext,
    classify_pair,
    is_chain_breaking,
    plan_chain_legs,
)
from flight_identity import MergedFlight  # noqa: E402

UTC = timezone.utc


def _dt(h, mi=0, *, day=12):
    return datetime(2020, 7, day, h, mi, tzinfo=UTC)


def flight(dep, arr, sched_dep, *, sched_arr=None, fid=1):
    return MergedFlight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=sched_dep,
        scheduled_arr=sched_arr or (sched_dep + timedelta(hours=2)),
        live_dep=None,
        live_arr=None,
        code=None,
        byair_flight_ids=frozenset({fid}),
    )


# --- classify_pair ----------------------------------------------------------


def test_lodging_between_is_overnight_even_same_airport():
    a = flight("STN", "CPH", _dt(9))
    b = flight("CPH", "EWR", _dt(20))
    kind = classify_pair(a, b, PairContext(lodging_between=True))
    assert kind is ConnectionKind.OVERNIGHT
    assert is_chain_breaking(kind)


def test_lodging_between_overnight_takes_precedence_over_left_evidence():
    a = flight("CPH", "CPH", _dt(9))  # contrived same-airport with lodging
    b = flight("CPH", "EWR", _dt(20))
    # lodging precedence: even with left-terminal evidence, lodging => overnight
    kind = classify_pair(a, b, PairContext(lodging_between=True, operator_left_terminal=True))
    assert kind is ConnectionKind.OVERNIGHT


def test_different_airport_is_transfer():
    a = flight("LHR", "LHR", _dt(9))
    b = flight("LGW", "JFK", _dt(14))
    kind = classify_pair(a, b, PairContext())
    assert kind is ConnectionKind.DIFFERENT_AIRPORT_TRANSFER
    assert is_chain_breaking(kind)


def test_same_airport_no_evidence_is_airside_connection():
    a = flight("STN", "CPH", _dt(9))
    b = flight("CPH", "JFK", _dt(13))  # 4h layover, same airport
    kind = classify_pair(a, b, PairContext())
    assert kind is ConnectionKind.SAME_AIRPORT_CONNECTION
    assert not is_chain_breaking(kind)


def test_same_airport_long_gap_still_silent_without_evidence():
    # C2: same-airport defaults to silence at ANY gap length.
    a = flight("CPH", "CPH", _dt(9))
    b = flight("CPH", "JFK", _dt(21))  # 12h, but no lodging and no geo evidence
    kind = classify_pair(a, b, PairContext())
    assert kind is ConnectionKind.SAME_AIRPORT_CONNECTION


def test_same_airport_with_left_evidence_breaks_chain():
    a = flight("CPH", "CPH", _dt(9))
    b = flight("CPH", "JFK", _dt(18))
    kind = classify_pair(a, b, PairContext(operator_left_terminal=True))
    assert kind is ConnectionKind.SAME_AIRPORT_LEFT
    assert is_chain_breaking(kind)


# --- plan_chain_legs --------------------------------------------------------


def test_single_flight_yields_departure_and_arrival():
    legs = plan_chain_legs([flight("BNA", "JFK", _dt(9))], [])
    assert [leg.kind for leg in legs] == [LegKind.AIRPORT_DEPARTURE, LegKind.AIRPORT_ARRIVAL]


def test_same_airport_connection_suppresses_interior_legs():
    # BNA -> [STN connection] -> CPH: the owner's live case. One opening
    # departure (BNA), one closing arrival (CPH), NO interior legs.
    chain = [
        flight("BNA", "STN", _dt(6), fid=1),
        flight("STN", "CPH", _dt(11), fid=2),  # same-airport STN connection
    ]
    legs = plan_chain_legs(chain, [PairContext()])
    kinds = [leg.kind for leg in legs]
    assert kinds == [LegKind.AIRPORT_DEPARTURE, LegKind.AIRPORT_ARRIVAL]
    # opening departure attaches to the FIRST flight; closing arrival to the LAST
    assert legs[0].to_flight is chain[0]
    assert legs[1].from_flight is chain[1]


def test_two_same_airport_connections_zero_interior_drives():
    # Owner's 2020-07-12 itinerary shape: CPH and JFK connections -> zero drives
    # between; only the ground endpoints.
    chain = [
        flight("STN", "CPH", _dt(9), fid=1),
        flight("CPH", "JFK", _dt(13), fid=2),
        flight("JFK", "BNA", _dt(18), fid=3),
    ]
    legs = plan_chain_legs(chain, [PairContext(), PairContext()])
    assert [leg.kind for leg in legs] == [LegKind.AIRPORT_DEPARTURE, LegKind.AIRPORT_ARRIVAL]


def test_different_airport_transfer_in_the_middle():
    chain = [
        flight("BNA", "LHR", _dt(6), fid=1),
        flight("LGW", "JFK", _dt(16), fid=2),  # LHR->LGW transfer
    ]
    legs = plan_chain_legs(chain, [PairContext()])
    kinds = [leg.kind for leg in legs]
    assert kinds == [LegKind.AIRPORT_DEPARTURE, LegKind.AIRPORT_TRANSFER, LegKind.AIRPORT_ARRIVAL]
    transfer = legs[1]
    assert transfer.from_flight is chain[0] and transfer.to_flight is chain[1]


def test_overnight_decomposes_into_arrival_then_departure():
    chain = [
        flight("BNA", "CPH", _dt(6, day=12), fid=1),
        flight("CPH", "JFK", _dt(9, day=14), fid=2),  # 2 nights later, hotel between
    ]
    legs = plan_chain_legs(chain, [PairContext(lodging_between=True)])
    kinds = [leg.kind for leg in legs]
    # opening departure, arrival (to hotel), departure (from hotel), closing arrival
    assert kinds == [
        LegKind.AIRPORT_DEPARTURE,
        LegKind.AIRPORT_ARRIVAL,
        LegKind.AIRPORT_DEPARTURE,
        LegKind.AIRPORT_ARRIVAL,
    ]
    assert legs[1].from_flight is chain[0]  # arrival off the first flight
    assert legs[2].to_flight is chain[1]  # departure toward the second flight


def test_same_airport_left_generates_the_pair():
    chain = [
        flight("CPH", "CPH", _dt(9), fid=1),
        flight("CPH", "JFK", _dt(19), fid=2),
    ]
    legs = plan_chain_legs(chain, [PairContext(operator_left_terminal=True)])
    assert [leg.kind for leg in legs] == [
        LegKind.AIRPORT_DEPARTURE,
        LegKind.AIRPORT_ARRIVAL,
        LegKind.AIRPORT_DEPARTURE,
        LegKind.AIRPORT_ARRIVAL,
    ]


def test_context_count_mismatch_raises():
    chain = [flight("A", "B", _dt(9)), flight("B", "C", _dt(13))]
    try:
        plan_chain_legs(chain, [])  # needs 1 context, gave 0
    except ValueError as exc:
        assert "pair contexts" in str(exc)
    else:
        raise AssertionError("expected ValueError on context/chain length mismatch")


def test_empty_chain_yields_no_legs():
    assert plan_chain_legs([], []) == []
