"""Tests for the airport drive-block assembler (`airport_drive_reconcile.py`).

Deterministic fixtures only — hand-written flight-state records, canned byAir
airport payloads, and a fake Maps client returning fixed travel times. No live
calendar, no network, no real keys. These pin the status gating (which
direction, when), the byAir-truth instant selection, the routing-endpoint
choice, the config-override passthrough, and the per-leg error degradation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from airport_drive_reconcile import build_drive_blocks_for_flight  # noqa: E402
from byair_client import ByAirError  # noqa: E402
from maps_client import MapsError  # noqa: E402

CT = timezone(timedelta(hours=-5))

# Canned byAir get_airport payloads (trimmed to the consumed slice).
BNA = {
    "id": 20,
    "name": "Nashville International Airport",
    "code": "BNA",
    "countryFlag": "🇺🇸",
    "timezone": "America/Chicago",
    "delay": {"index": "low"},
}
LGA = {
    "id": 28,
    "name": "LaGuardia Airport",
    "code": "LGA",
    "countryFlag": "🇺🇸",
    "timezone": "America/New_York",
    "delay": {"index": "low"},
}
CDG = {
    "id": 99,
    "name": "Paris Charles de Gaulle",
    "code": "CDG",
    "countryFlag": "🇫🇷",
    "timezone": "Europe/Paris",
    "delay": {"index": "low"},
}
AIRPORTS = {20: BNA, 28: LGA, 99: CDG}

HOME = "1 Infinite Loop, Cupertino, CA"


@dataclass
class _FakeTravelTime:
    in_traffic_seconds: int | None
    duration_seconds: int


class FakeByAir:
    """Serves canned airport payloads; can be told to fail for one id."""

    def __init__(self, airports=None, fail_ids: frozenset[int] | set[int] = frozenset()):
        self.airports = dict(AIRPORTS if airports is None else airports)
        self.fail_ids = set(fail_ids)
        self.calls: list[int] = []

    def get_airport(self, airport_id: int) -> dict:
        self.calls.append(airport_id)
        if airport_id in self.fail_ids:
            raise ByAirError("not_found", f"airport {airport_id} not found")
        return self.airports[airport_id]


class FakeMaps:
    """Returns a fixed travel time; can be told to raise for any route."""

    def __init__(self, seconds=1800, in_traffic=None, fail=False):
        self._seconds = seconds
        self._in_traffic = in_traffic
        self._fail = fail
        self.routes: list[tuple[str, str]] = []

    def travel_time(self, *, origin: str, destination: str) -> _FakeTravelTime:
        self.routes.append((origin, destination))
        if self._fail:
            raise MapsError("ZERO_RESULTS", "no route")
        return _FakeTravelTime(in_traffic_seconds=self._in_traffic, duration_seconds=self._seconds)


def _state(**overrides) -> dict:
    state = {
        "flight_id": 12345,
        "code": "DL123",
        "scheduled_dep_time": "2026-07-02T14:00:00-05:00",
        "scheduled_arr_time": "2026-07-02T16:30:00-05:00",
        "dep_airport_id": 20,  # BNA
        "arr_airport_id": 28,  # LGA
        "last_snapshot": {"computed_status": "scheduled"},
    }
    state.update(overrides)
    return state


def _snapshot(status, **extra) -> dict:
    snap = {"computed_status": status}
    snap.update(extra)
    return snap


# --- to_airport gating + assembly ----------------------------------------------


def test_to_airport_built_before_departure():
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin="36.1,-86.6", home_address=HOME
    )
    assert len(blocks) == 1
    block = blocks[0]
    assert block.direction == "to_airport"
    assert block.summary == "Drive: → BNA (DL123)"
    # Domestic (US→US) → 60-min clearance; leave-by = anchor − routed drive.
    dep = datetime(2026, 7, 2, 14, 0, tzinfo=CT)
    assert block.anchor == dep - timedelta(minutes=60)
    assert block.leg_start == block.anchor - timedelta(seconds=1800)
    assert block.origin == "36.1,-86.6"
    assert block.destination == "Nashville International Airport"  # name, routable + readable
    assert block.timezone == "America/Chicago"
    assert maps.routes == [("36.1,-86.6", "Nashville International Airport")]


def test_to_airport_uses_in_traffic_seconds_when_present():
    byair, maps = FakeByAir(), FakeMaps(seconds=1800, in_traffic=2400)
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert blocks[0].baseline_seconds == 2400


@pytest.mark.parametrize("status", ["scheduled", "check_in_open", "boarding"])
def test_to_airport_statuses(status):
    byair, maps = FakeByAir(), FakeMaps()
    blocks = build_drive_blocks_for_flight(
        _state(last_snapshot=_snapshot(status)),
        byair=byair,
        maps=maps,
        origin="home",
        home_address=HOME,
    )
    assert [b.direction for b in blocks] == ["to_airport"]


def test_to_airport_skipped_without_origin():
    byair, maps = FakeByAir(), FakeMaps()
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin=None, home_address=HOME
    )
    assert blocks == []
    assert maps.routes == []  # never routed — short-circuited before the maps call


# --- from_airport gating + assembly --------------------------------------------


@pytest.mark.parametrize("status", ["departed", "en_route", "landed"])
def test_from_airport_built_after_departure(status):
    byair, maps = FakeByAir(), FakeMaps(seconds=1200)
    blocks = build_drive_blocks_for_flight(
        _state(last_snapshot=_snapshot(status)),
        byair=byair,
        maps=maps,
        origin="home",
        home_address=HOME,
    )
    assert len(blocks) == 1
    block = blocks[0]
    assert block.direction == "from_airport"
    assert block.summary == "Drive: LGA → home"
    # Domestic arrival → 20 min after arr; drive home starts at the anchor.
    arr = datetime(2026, 7, 2, 16, 30, tzinfo=CT)
    assert block.anchor == arr + timedelta(minutes=20)
    assert block.leg_start == block.anchor
    assert block.leg_end == block.anchor + timedelta(seconds=1200)
    assert block.origin == "LaGuardia Airport"
    assert block.destination == HOME
    assert block.timezone == "America/New_York"
    assert maps.routes == [("LaGuardia Airport", HOME)]


def test_from_airport_skipped_without_home_address():
    byair, maps = FakeByAir(), FakeMaps()
    blocks = build_drive_blocks_for_flight(
        _state(last_snapshot=_snapshot("landed")),
        byair=byair,
        maps=maps,
        origin="home",
        home_address=None,
    )
    assert blocks == []
    assert maps.routes == []


def test_from_airport_uses_live_snapshot_arr_time_over_scheduled():
    byair, maps = FakeByAir(), FakeMaps(seconds=1200)
    # In flight, byAir publishes a delayed arr_time; the block anchors on it.
    snap = _snapshot("en_route", arr_time="2026-07-02T17:30:00-05:00")
    blocks = build_drive_blocks_for_flight(
        _state(last_snapshot=snap), byair=byair, maps=maps, origin="home", home_address=HOME
    )
    delayed_arr = datetime(2026, 7, 2, 17, 30, tzinfo=CT)
    assert blocks[0].anchor == delayed_arr + timedelta(minutes=20)


# --- classification via both airports ------------------------------------------


def test_international_arrival_uses_120_min_clearance_on_departure_block():
    # BNA → CDG (US → FR): international departure → 120-min clearance.
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    state = _state(arr_airport_id=99)  # CDG
    blocks = build_drive_blocks_for_flight(
        state, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    dep = datetime(2026, 7, 2, 14, 0, tzinfo=CT)
    assert blocks[0].anchor == dep - timedelta(minutes=120)


def test_intl_to_us_arrival_uses_40_min_post_arrival():
    # CDG → BNA (FR → US): intl into the US → 40-min post-arrival.
    byair, maps = FakeByAir(), FakeMaps(seconds=1200)
    state = _state(dep_airport_id=99, arr_airport_id=20, last_snapshot=_snapshot("landed"))
    blocks = build_drive_blocks_for_flight(
        state, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    arr = datetime(2026, 7, 2, 16, 30, tzinfo=CT)
    assert blocks[0].anchor == arr + timedelta(minutes=40)


# --- config overrides ----------------------------------------------------------


def test_config_override_flows_through_to_clearance():
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    config = {"airport_clearance_domestic_minutes": 45}
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin="home", home_address=HOME, config=config
    )
    dep = datetime(2026, 7, 2, 14, 0, tzinfo=CT)
    assert blocks[0].anchor == dep - timedelta(minutes=45)


# --- gating: nothing built -----------------------------------------------------


def test_no_maps_client_builds_nothing():
    byair = FakeByAir()
    assert (
        build_drive_blocks_for_flight(
            _state(), byair=byair, maps=None, origin="home", home_address=HOME
        )
        == []
    )
    assert byair.calls == []  # short-circuits before any airport lookup


def test_no_byair_client_builds_nothing_without_raising():
    # A None byair must not AttributeError in the airport lookup — the
    # never-raises contract treats it the same as a None maps client.
    maps = FakeMaps()
    assert (
        build_drive_blocks_for_flight(
            _state(), byair=None, maps=maps, origin="home", home_address=HOME
        )
        == []
    )
    assert maps.routes == []


@pytest.mark.parametrize("status", ["cancelled", "diverted"])
def test_cancelled_or_diverted_builds_nothing(status):
    byair, maps = FakeByAir(), FakeMaps()
    blocks = build_drive_blocks_for_flight(
        _state(last_snapshot=_snapshot(status)),
        byair=byair,
        maps=maps,
        origin="home",
        home_address=HOME,
    )
    assert blocks == []


# --- per-leg error degradation -------------------------------------------------


def test_departure_block_dropped_when_primary_airport_lookup_fails():
    # The departure airport (primary for to_airport) fails to resolve → no block.
    byair = FakeByAir(fail_ids={20})
    maps = FakeMaps()
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert blocks == []


def test_departure_block_built_when_only_secondary_airport_lookup_fails():
    # The arrival airport (secondary for to_airport) fails → still build, and the
    # missing arrival flag classifies the route international (the safe fallback).
    byair = FakeByAir(fail_ids={28})
    maps = FakeMaps(seconds=1800)
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert len(blocks) == 1
    dep = datetime(2026, 7, 2, 14, 0, tzinfo=CT)
    assert blocks[0].anchor == dep - timedelta(minutes=120)  # international fallback


def test_block_dropped_when_routing_fails():
    byair, maps = FakeByAir(), FakeMaps(fail=True)
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert blocks == []


# --- blank endpoints read as absent (never reach routing) ----------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_origin_skips_to_airport_without_routing(blank):
    # A blank resolved origin must read as absent, not a routable string —
    # MapsClient.travel_time raises ValueError on an empty endpoint.
    byair, maps = FakeByAir(), FakeMaps()
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin=blank, home_address=HOME
    )
    assert blocks == []
    assert maps.routes == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_home_address_skips_from_airport_without_routing(blank):
    byair, maps = FakeByAir(), FakeMaps()
    blocks = build_drive_blocks_for_flight(
        _state(last_snapshot=_snapshot("landed")),
        byair=byair,
        maps=maps,
        origin="home",
        home_address=blank,
    )
    assert blocks == []
    assert maps.routes == []


# --- snapshot-time fallback (bad byAir data) -----------------------------------


def test_empty_snapshot_dep_time_falls_back_to_scheduled():
    # A present-but-empty actual time must not suppress the block — fall back to
    # the scheduled time rather than dropping it.
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    snap = _snapshot("scheduled", dep_time="")
    blocks = build_drive_blocks_for_flight(
        _state(last_snapshot=snap), byair=byair, maps=maps, origin="home", home_address=HOME
    )
    dep = datetime(2026, 7, 2, 14, 0, tzinfo=CT)  # scheduled
    assert blocks[0].anchor == dep - timedelta(minutes=60)


def test_unparseable_snapshot_arr_time_falls_back_to_scheduled():
    byair, maps = FakeByAir(), FakeMaps(seconds=1200)
    snap = _snapshot("landed", arr_time="not-a-timestamp")
    blocks = build_drive_blocks_for_flight(
        _state(last_snapshot=snap), byair=byair, maps=maps, origin="home", home_address=HOME
    )
    arr = datetime(2026, 7, 2, 16, 30, tzinfo=CT)  # scheduled
    assert blocks[0].anchor == arr + timedelta(minutes=20)


def test_block_dropped_when_both_snapshot_and_scheduled_times_unusable():
    byair, maps = FakeByAir(), FakeMaps()
    snap = _snapshot("scheduled", dep_time="garbage")
    state = _state(last_snapshot=snap, scheduled_dep_time="also-garbage")
    blocks = build_drive_blocks_for_flight(
        state, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert blocks == []


# --- routing seconds: 0 in-traffic is a valid time -----------------------------


def test_zero_in_traffic_seconds_is_used_not_treated_as_missing():
    byair, maps = FakeByAir(), FakeMaps(seconds=1800, in_traffic=0)
    blocks = build_drive_blocks_for_flight(
        _state(), byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert blocks[0].baseline_seconds == 0  # the 0 in-traffic estimate, not 1800
