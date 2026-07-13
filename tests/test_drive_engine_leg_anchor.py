"""Tests for leg anchor derivation (the §B buffer math).

Deterministic fixtures only — fixed tz-aware datetimes, hand-built MergedFlights,
and real `countryFlag` emoji, no wall-clock. These pin the §B anchors: departure
= effective_dep − clearance, arrival = effective_arr + post_arrival, transfer =
the window between them, with buffer policy delegated to airport_lead (domestic
60 / intl 120 clearance + delay nudge; post-arrival 20 / 40 / 60).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from chain import LegKind, PlannedLeg  # noqa: E402
from flight_identity import MergedFlight  # noqa: E402
from leg_anchor import (  # noqa: E402
    AirportFacts,
    BufferOverrides,
    resolve_leg_anchor,
)

UTC = timezone.utc
US = "🇺🇸"
GB = "🇬🇧"
DK = "🇩🇰"


def _dt(h, mi=0, *, day=12):
    return datetime(2020, 7, day, h, mi, tzinfo=UTC)


def flight(dep, arr, sched_dep, sched_arr, *, fid=1, live_dep=None, live_arr=None):
    return MergedFlight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=sched_dep,
        scheduled_arr=sched_arr,
        live_dep=live_dep,
        live_arr=live_arr,
        code=None,
        byair_flight_ids=frozenset({fid}),
    )


# --- departure: arrive_by = effective_dep − clearance -----------------------


def test_departure_domestic_clearance_60():
    f = flight("BNA", "JFK", _dt(9), _dt(11))
    leg = PlannedLeg(LegKind.AIRPORT_DEPARTURE, to_flight=f)
    concrete = resolve_leg_anchor(
        leg, facts=AirportFacts(dep_flag=US, arr_flag=US, delay_index="low")
    )
    # A departure leg drives TO the flight's departure airport (BNA), not its
    # destination — you go to the airport you fly out of.
    assert concrete.dest_airport == "BNA"
    assert concrete.anchor == _dt(9) - timedelta(minutes=60)


def test_departure_international_clearance_120_plus_high_delay_nudge():
    f = flight("JFK", "LHR", _dt(20), _dt(23, 59))
    leg = PlannedLeg(LegKind.AIRPORT_DEPARTURE, to_flight=f)
    concrete = resolve_leg_anchor(
        leg, facts=AirportFacts(dep_flag=US, arr_flag=GB, delay_index="high")
    )
    # 120 international + 30 high nudge = 150
    assert concrete.anchor == _dt(20) - timedelta(minutes=150)


def test_departure_uses_live_time_when_present():
    f = flight("BNA", "JFK", _dt(9), _dt(11), live_dep=_dt(9, 40))
    leg = PlannedLeg(LegKind.AIRPORT_DEPARTURE, to_flight=f)
    concrete = resolve_leg_anchor(
        leg, facts=AirportFacts(dep_flag=US, arr_flag=US, delay_index="low")
    )
    assert concrete.anchor == _dt(9, 40) - timedelta(minutes=60)


# --- arrival: depart_after = effective_arr + post_arrival -------------------


def test_arrival_domestic_post_20():
    f = flight("BNA", "JFK", _dt(9), _dt(11))
    leg = PlannedLeg(LegKind.AIRPORT_ARRIVAL, from_flight=f)
    concrete = resolve_leg_anchor(leg, facts=AirportFacts(dep_flag=US, arr_flag=US))
    assert concrete.origin_airport == "JFK"
    assert concrete.anchor == _dt(11) + timedelta(minutes=20)


def test_arrival_intl_into_us_post_40():
    f = flight("LHR", "JFK", _dt(9), _dt(12))
    leg = PlannedLeg(LegKind.AIRPORT_ARRIVAL, from_flight=f)
    concrete = resolve_leg_anchor(leg, facts=AirportFacts(dep_flag=GB, arr_flag=US))
    assert concrete.anchor == _dt(12) + timedelta(minutes=40)


def test_arrival_intl_abroad_post_60():
    f = flight("JFK", "LHR", _dt(9), _dt(21))
    leg = PlannedLeg(LegKind.AIRPORT_ARRIVAL, from_flight=f)
    concrete = resolve_leg_anchor(leg, facts=AirportFacts(dep_flag=US, arr_flag=GB))
    assert concrete.anchor == _dt(21) + timedelta(minutes=60)


def test_arrival_uses_live_arrival_when_present():
    f = flight("BNA", "JFK", _dt(9), _dt(11), live_arr=_dt(11, 25))
    leg = PlannedLeg(LegKind.AIRPORT_ARRIVAL, from_flight=f)
    concrete = resolve_leg_anchor(leg, facts=AirportFacts(dep_flag=US, arr_flag=US))
    assert concrete.anchor == _dt(11, 25) + timedelta(minutes=20)


# --- transfer: window between landing+buffer and next-dep−clearance ----------


def test_transfer_window_endpoints():
    n = flight("LHR", "LHR", _dt(9), _dt(10), fid=1)  # lands LHR 10:00
    n1 = flight("LGW", "JFK", _dt(16), _dt(19), fid=2)  # departs LGW 16:00
    leg = PlannedLeg(LegKind.AIRPORT_TRANSFER, from_flight=n, to_flight=n1)
    concrete = resolve_leg_anchor(
        leg,
        facts=AirportFacts(dep_flag=US, arr_flag=GB),  # N arrived abroad (GB): post 60
        partner_facts=AirportFacts(
            dep_flag=GB, arr_flag=US, delay_index="low"
        ),  # N+1 intl dep: 120
    )
    assert concrete.origin_airport == "LHR"
    assert concrete.dest_airport == "LGW"
    assert concrete.window_start == _dt(10) + timedelta(minutes=60)
    assert concrete.window_end == _dt(16) - timedelta(minutes=120)


def test_transfer_requires_partner_facts():
    n = flight("LHR", "LHR", _dt(9), _dt(10), fid=1)
    n1 = flight("LGW", "JFK", _dt(16), _dt(19), fid=2)
    leg = PlannedLeg(LegKind.AIRPORT_TRANSFER, from_flight=n, to_flight=n1)
    with pytest.raises(ValueError, match="partner_facts"):
        resolve_leg_anchor(leg, facts=AirportFacts(dep_flag=US, arr_flag=GB))


# --- overrides + error paths ------------------------------------------------


def test_config_override_clearance():
    f = flight("BNA", "JFK", _dt(9), _dt(11))
    leg = PlannedLeg(LegKind.AIRPORT_DEPARTURE, to_flight=f)
    concrete = resolve_leg_anchor(
        leg,
        facts=AirportFacts(dep_flag=US, arr_flag=US, delay_index="low"),
        overrides=BufferOverrides(clearance_domestic=90),
    )
    assert concrete.anchor == _dt(9) - timedelta(minutes=90)


def test_arrival_without_arr_time_raises():
    f = MergedFlight(
        dep_airport="BNA",
        arr_airport="JFK",
        scheduled_dep=_dt(9),
        scheduled_arr=None,
        live_dep=None,
        live_arr=None,
        code=None,
        byair_flight_ids=frozenset({1}),
    )
    leg = PlannedLeg(LegKind.AIRPORT_ARRIVAL, from_flight=f)
    with pytest.raises(ValueError, match="arrival time"):
        resolve_leg_anchor(leg, facts=AirportFacts(dep_flag=US, arr_flag=US))


def test_departure_missing_flight_raises():
    leg = PlannedLeg(LegKind.AIRPORT_DEPARTURE, to_flight=None)
    with pytest.raises(ValueError, match="missing to_flight"):
        resolve_leg_anchor(leg, facts=AirportFacts())
