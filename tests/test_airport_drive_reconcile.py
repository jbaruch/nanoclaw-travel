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

from airport_block import build_description  # noqa: E402
from airport_drive_reconcile import (  # noqa: E402
    build_drive_blocks_for_flight,
    run_airport_drive_reconcile,
)
from byair_client import ByAirError  # noqa: E402
from composio_client import ComposioError  # noqa: E402
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


# === orchestration: run_airport_drive_reconcile ================================

TO_ANCHOR = datetime(2026, 7, 2, 13, 0, tzinfo=CT)  # dep 14:00 − 60-min domestic clearance


class FakeComposio:
    """Records create/delete calls; serves find_events filtered by the window.

    `find_events` returns only events whose `start.dateTime` falls within the
    requested `[timeMin, timeMax]` — like the real toolkit — so a test can prove
    the fetch window is wide enough to surface a stale block (and that the run
    shifts it rather than creating a duplicate).
    """

    def __init__(self, events=None, *, create_fail=False, delete_404=False):
        self._events = list(events or [])
        self.created: list[dict] = []
        self.deleted: list[dict] = []
        self.find_called = 0
        self._create_fail = create_fail
        self._delete_404 = delete_404

    def find_events(self, arguments: dict) -> dict:
        self.find_called += 1
        self.find_args = arguments
        lo = datetime.fromisoformat(arguments["timeMin"].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(arguments["timeMax"].replace("Z", "+00:00"))
        items = []
        for event in self._events:
            start = event.get("start", {}).get("dateTime")
            if start is None:
                items.append(event)
                continue
            if lo <= datetime.fromisoformat(start) <= hi:
                items.append(event)
        return {"items": items}

    def create_event(self, arguments: dict) -> dict:
        if self._create_fail:
            raise ComposioError("create boom", status_code=500)
        self.created.append(arguments)
        return {"id": f"evt_new_{len(self.created)}"}

    def delete_event(self, arguments: dict) -> dict:
        if self._delete_404:
            raise ComposioError("already gone", status_code=404)
        self.deleted.append(arguments)
        return {}


def _existing_to_airport_event(*, anchor=TO_ANCHOR, baseline=1800, event_id="evt_old"):
    """A fetched primary-calendar event carrying a to_airport block's state."""
    desc = build_description(
        summary="Drive: → BNA (DL123)",
        flight_id="12345",
        direction="to_airport",
        baseline_seconds=baseline,
        anchor=anchor,
        origin="home",
        destination="Nashville International Airport",
    )
    start = anchor - timedelta(seconds=baseline)
    return {
        "id": event_id,
        "summary": "Drive: → BNA (DL123)",
        "description": desc,
        "calendar_id": "primary",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": anchor.isoformat()},
    }


def test_run_creates_block_when_none_exists():
    composio = FakeComposio(events=[])
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    result = run_airport_drive_reconcile(
        [_state()], composio=composio, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert result["status"] == "ok"
    assert result["planned"] == 1 and result["executed"] == 1
    assert composio.find_called == 1
    assert len(composio.created) == 1
    assert composio.created[0]["summary"] == "Drive: → BNA (DL123)"
    assert composio.deleted == []


def test_run_suppresses_unchanged_block():
    composio = FakeComposio(events=[_existing_to_airport_event()])
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    result = run_airport_drive_reconcile(
        [_state()], composio=composio, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert result["suppressed"] == 1
    assert result["executed"] == 0
    assert composio.created == [] and composio.deleted == []


def test_run_suppresses_subthreshold_traffic_drift():
    # Existing block routed at 1800s; fresh route 1860s → leave-by drifts 60s < 5min.
    composio = FakeComposio(events=[_existing_to_airport_event(baseline=1800)])
    byair, maps = FakeByAir(), FakeMaps(seconds=1860)
    result = run_airport_drive_reconcile(
        [_state()], composio=composio, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert result["suppressed"] == 1
    assert composio.created == [] and composio.deleted == []


def test_run_shifts_block_past_threshold_via_delete_and_recreate():
    # Flight delayed 30 min: existing anchor 13:00 (old dep 14:00), new dep 14:30
    # → desired anchor 13:30, leave-by drifts 30 min > threshold.
    composio = FakeComposio(events=[_existing_to_airport_event(event_id="evt_old")])
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    state = _state(scheduled_dep_time="2026-07-02T14:30:00-05:00")
    result = run_airport_drive_reconcile(
        [state], composio=composio, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert result["executed"] == 1
    assert composio.deleted == [{"calendar_id": "primary", "event_id": "evt_old"}]
    assert len(composio.created) == 1
    # Recreated via build_block_args → carries the timezone (correct-instant create).
    assert composio.created[0]["timezone"] == "America/Chicago"


def test_run_collects_create_failure_without_raising():
    composio = FakeComposio(events=[], create_fail=True)
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    result = run_airport_drive_reconcile(
        [_state()], composio=composio, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert result["executed"] == 0
    assert result["failed"] == [{"flight_id": 12345, "op": "create", "kind": "airport_drive_dep"}]


def test_run_tolerates_delete_404_on_shift():
    composio = FakeComposio(events=[_existing_to_airport_event()], delete_404=True)
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    state = _state(scheduled_dep_time="2026-07-02T14:30:00-05:00")
    result = run_airport_drive_reconcile(
        [state], composio=composio, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    # Delete 404'd (already gone) → idempotent; the recreate still ran.
    assert result["executed"] == 1
    assert len(composio.created) == 1


def test_run_with_no_desired_blocks_skips_the_fetch():
    composio = FakeComposio(events=[])
    byair, maps = FakeByAir(), FakeMaps()
    result = run_airport_drive_reconcile(
        [_state(last_snapshot=_snapshot("cancelled"))],
        composio=composio,
        byair=byair,
        maps=maps,
        origin="home",
        home_address=HOME,
    )
    assert result == {"status": "ok", "planned": 0, "executed": 0, "suppressed": 0, "failed": []}
    assert composio.find_called == 0  # no desired blocks → never hit the calendar


# --- from_airport orchestration: end-time (duration) drift must shift ----------

FA_ANCHOR = datetime(2026, 7, 2, 16, 50, tzinfo=CT)  # arr 16:30 + 20-min domestic post-arrival


def _existing_from_airport_event(*, anchor=FA_ANCHOR, baseline=1200, event_id="evt_arr"):
    """A fetched primary-calendar event carrying a from_airport block's state."""
    desc = build_description(
        summary="Drive: LGA → home",
        flight_id="12345",
        direction="from_airport",
        baseline_seconds=baseline,
        anchor=anchor,
        origin="LaGuardia Airport",
        destination=HOME,
    )
    end = anchor + timedelta(seconds=baseline)
    return {
        "id": event_id,
        "summary": "Drive: LGA → home",
        "description": desc,
        "calendar_id": "primary",
        "start": {"dateTime": anchor.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


def test_run_shifts_from_airport_block_when_only_duration_changes_past_threshold():
    # Same arrival anchor (so leg_start is unchanged), but the routed drive home
    # grows 20 → 40 min: the END moves 20 min, which must shift the block. A
    # start-only comparison would wrongly read this as zero drift.
    composio = FakeComposio(events=[_existing_from_airport_event(baseline=1200)])
    byair, maps = FakeByAir(), FakeMaps(seconds=2400)
    result = run_airport_drive_reconcile(
        [_state(last_snapshot=_snapshot("landed"))],
        composio=composio,
        byair=byair,
        maps=maps,
        origin="home",
        home_address=HOME,
    )
    assert result["suppressed"] == 0
    assert result["executed"] == 1
    assert composio.deleted == [{"calendar_id": "primary", "event_id": "evt_arr"}]
    assert len(composio.created) == 1


def test_run_suppresses_from_airport_subthreshold_duration_drift():
    # Drive home grows by 60s (1200 → 1260) → end drifts < 5 min → stay put.
    composio = FakeComposio(events=[_existing_from_airport_event(baseline=1200)])
    byair, maps = FakeByAir(), FakeMaps(seconds=1260)
    result = run_airport_drive_reconcile(
        [_state(last_snapshot=_snapshot("landed"))],
        composio=composio,
        byair=byair,
        maps=maps,
        origin="home",
        home_address=HOME,
    )
    assert result["suppressed"] == 1
    assert composio.created == [] and composio.deleted == []


# --- fetch window: a stale block far outside the desired window is still found --


def test_run_shifts_not_duplicates_a_block_left_far_outside_the_desired_window():
    # The block was created at the scheduled time (13:00), then the flight was
    # delayed 3h (dep 17:00). The old block sits hours before the new desired
    # window — a desired-window-only fetch would miss it and create a DUPLICATE.
    # Anchored on the scheduled times, the fetch still surfaces it, so the run
    # shifts (delete + recreate) the one block instead.
    old = _existing_to_airport_event(anchor=TO_ANCHOR)  # start 12:30, at the scheduled time
    composio = FakeComposio(events=[old])
    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    state = _state(last_snapshot=_snapshot("scheduled", dep_time="2026-07-02T17:00:00-05:00"))
    result = run_airport_drive_reconcile(
        [state], composio=composio, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    assert composio.deleted == [{"calendar_id": "primary", "event_id": "evt_old"}]
    assert len(composio.created) == 1  # one block, shifted — not two
    assert result["executed"] == 1


def test_fetch_window_spans_scheduled_time_under_a_large_delay():
    # Unit-level guard on the window: a 3h-delayed departure's window still
    # reaches back to cover a block at the scheduled time.
    from airport_drive_reconcile import _fetch_window  # noqa: PLC0415

    byair, maps = FakeByAir(), FakeMaps(seconds=1800)
    state = _state(last_snapshot=_snapshot("scheduled", dep_time="2026-07-02T17:00:00-05:00"))
    blocks = build_drive_blocks_for_flight(
        state, byair=byair, maps=maps, origin="home", home_address=HOME
    )
    time_min, _ = _fetch_window([state], blocks)
    lo = datetime.fromisoformat(time_min.replace("Z", "+00:00"))
    old_block_start = TO_ANCHOR - timedelta(seconds=1800)  # the stale block's start
    assert lo <= old_block_start
