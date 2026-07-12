"""Tests for chain assembly (grouping + PairContext derivation).

Deterministic fixtures only — hand-built merged flights and schedules, no
wall-clock. These pin: trip grouping and ordering, the lodging-between check that
distinguishes an overnight from a connection, and the left-terminal predicate
threading.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from chain_builder import (  # noqa: E402
    build_pair_contexts,
    group_into_chains,
    has_lodging_between,
)
from flight_identity import MergedFlight  # noqa: E402

UTC = timezone.utc


def _dt(h, mi=0, *, day=12):
    return datetime(2026, 7, day, h, mi, tzinfo=UTC)


def flight(dep, arr, sched_dep, sched_arr, *, fid=1, trip_id=None):
    return MergedFlight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=sched_dep,
        scheduled_arr=sched_arr,
        live_dep=None,
        live_arr=None,
        code=None,
        byair_flight_ids=frozenset({fid}),
        trip_id=trip_id,
    )


# --- grouping ---------------------------------------------------------------


def test_same_trip_flights_form_one_ordered_chain():
    f2 = flight("CPH", "JFK", _dt(13), _dt(20), fid=2, trip_id=99)
    f1 = flight("STN", "CPH", _dt(9), _dt(11), fid=1, trip_id=99)
    chains = group_into_chains([f2, f1])
    assert len(chains) == 1
    assert [f.dep_airport for f in chains[0]] == ["STN", "CPH"]  # ordered by dep


def test_no_trip_id_is_singleton_chain():
    f1 = flight("BNA", "JFK", _dt(9), _dt(11), fid=1, trip_id=None)
    f2 = flight("LAX", "SFO", _dt(15), _dt(16), fid=2, trip_id=None)
    chains = group_into_chains([f2, f1])
    assert [len(c) for c in chains] == [1, 1]
    assert chains[0][0].dep_airport == "BNA"  # ordered by dep across chains


def test_distinct_trips_are_separate_chains():
    a = flight("STN", "CPH", _dt(9), _dt(11), fid=1, trip_id=1)
    b = flight("BNA", "JFK", _dt(15), _dt(18), fid=2, trip_id=2)
    chains = group_into_chains([b, a])
    assert len(chains) == 2
    assert chains[0][0].trip_id == 1  # earlier-departing trip first


# --- lodging-between --------------------------------------------------------


def test_lodging_between_true_when_checkin_in_gap():
    schedule = [{"type": "Lodging", "start": "2026-07-12T15:00:00Z", "location": "Hotel"}]
    assert has_lodging_between(schedule, _dt(11), _dt(20))


def test_lodging_between_false_when_checkin_outside_gap():
    schedule = [{"type": "Lodging", "start": "2026-07-13T15:00:00Z", "location": "Hotel"}]
    assert not has_lodging_between(schedule, _dt(11), _dt(20))


def test_lodging_between_ignores_non_lodging_and_bad_dates():
    schedule = [
        {"type": "Flight", "start": "2026-07-12T15:00:00Z"},
        {"type": "Lodging", "start": "not-a-date"},
    ]
    assert not has_lodging_between(schedule, _dt(11), _dt(20))


def test_lodging_between_empty_or_none_schedule():
    assert not has_lodging_between(None, _dt(11), _dt(20))
    assert not has_lodging_between([], _dt(11), _dt(20))


# --- pair contexts ----------------------------------------------------------


def test_pair_context_overnight_from_lodging():
    chain = [
        flight("BNA", "CPH", _dt(6, day=12), _dt(9, day=12), fid=1, trip_id=1),
        flight("CPH", "JFK", _dt(9, day=14), _dt(12, day=14), fid=2, trip_id=1),
    ]
    schedule = [{"type": "Lodging", "start": "2026-07-12T18:00:00Z", "location": "Hotel"}]
    ctxs = build_pair_contexts(chain, schedule=schedule)
    assert len(ctxs) == 1
    assert ctxs[0].lodging_between is True
    assert ctxs[0].operator_left_terminal is False


def test_pair_context_left_terminal_predicate_threaded():
    chain = [
        flight("CPH", "CPH", _dt(9), _dt(10), fid=1, trip_id=1),
        flight("CPH", "JFK", _dt(18), _dt(22), fid=2, trip_id=1),
    ]
    ctxs = build_pair_contexts(chain, schedule=[], left_terminal=lambda a, b: True)
    assert ctxs[0].operator_left_terminal is True
    assert ctxs[0].lodging_between is False


def test_pair_context_connection_no_lodging_no_geo():
    chain = [
        flight("STN", "CPH", _dt(9), _dt(11), fid=1, trip_id=1),
        flight("CPH", "JFK", _dt(13), _dt(20), fid=2, trip_id=1),
    ]
    ctxs = build_pair_contexts(chain, schedule=[])
    assert ctxs[0].lodging_between is False
    assert ctxs[0].operator_left_terminal is False


def test_single_flight_chain_has_no_pairs():
    chain = [flight("BNA", "JFK", _dt(9), _dt(11), fid=1, trip_id=1)]
    assert build_pair_contexts(chain, schedule=[]) == []
