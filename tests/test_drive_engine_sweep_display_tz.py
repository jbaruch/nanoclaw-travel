"""Seam test: `_run_sweep` hands the operator's zone to the meeting planner (#301).

`meeting_desired_blocks(display_tz=...)` and the apply render are unit-tested in
their own files, but the one production line that reads the operator zone and
passes it on lives inside `_run_sweep`, which builds its live clients inline.
Copilot on #308 asked for that wiring to be pinned, deferred here (#311).

Every live client `_run_sweep` constructs is stubbed at its source module, so
the sweep runs end to end over an empty itinerary and an empty calendar. The
test captures the `display_tz` the meeting planner receives for an available
reader result and for `None`. The sweep's clock is frozen at a fixed instant
(`reconcile_sweep.datetime`), so its scan and calendar windows never depend on
the day the suite runs.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Self

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

import byair_client  # noqa: E402
import fetch_events  # noqa: E402
import google_calendar_client  # noqa: E402
import maps_client  # noqa: E402
import operator_tz  # noqa: E402
import reconcile_sweep  # noqa: E402
import skip_state  # noqa: E402
import state  # noqa: E402
import trip_origin  # noqa: E402
from operator_tz import OperatorTz  # noqa: E402
from reconcile import ReconcilePlan  # noqa: E402


class _FrozenDatetime(datetime):
    """`datetime` whose `now()` is a fixed instant: 2026-09-11 13:00 UTC."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        frozen = cls(2026, 9, 11, 13, 0, tzinfo=UTC)
        return frozen if tz is None else frozen.astimezone(tz)


class _EmptyCalendar:
    def find_events(self, _arguments):
        return {"items": []}


class _EmptyFetcher:
    def fetch_window(self, *, time_min, time_max):
        return []


def _stub_live_clients(monkeypatch, reader, state_dir: Path) -> dict:
    """Stub every live dependency of `_run_sweep`; return the capture dict."""
    captured: dict = {}

    # The drive-decision and skip stores resolve through this env var; the
    # sweep's lodging side prunes the decision store on every run, so point
    # both at a throwaway directory, never the deployed state (#312).
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(state_dir))

    # The sweep reads `datetime.now(timezone.utc)` for its windows (policy
    # review on #311: freeze it, whatever the assertions read).
    monkeypatch.setattr(reconcile_sweep, "datetime", _FrozenDatetime)

    # Function-local imports inside `_run_sweep` read these module attributes
    # at call time.
    monkeypatch.setattr(operator_tz, "read_operator_tz", reader)
    monkeypatch.setattr(state, "read_config", lambda: {"home_address": "12 Example St"})
    monkeypatch.setattr(state, "read_active_flights", lambda: [])
    monkeypatch.setattr(state, "read_flight_state", lambda _flight_id: None)
    monkeypatch.setattr(trip_origin, "load_travel_schedule", lambda: [])
    monkeypatch.setattr(skip_state, "load_active_skips", lambda _now: {})
    monkeypatch.setattr(maps_client.MapsClient, "from_env", classmethod(lambda cls, **_: None))
    monkeypatch.setattr(byair_client.ByAirClient, "from_env", classmethod(lambda cls, **_: None))
    monkeypatch.setattr(google_calendar_client, "GoogleCalendarClient", _EmptyCalendar)
    monkeypatch.setattr(fetch_events, "CalendarFetcher", _EmptyFetcher)

    # Module-level collaborators of the sweep that touch disk or the network.
    monkeypatch.setattr(reconcile_sweep, "load_verdicts", lambda _now: {})
    monkeypatch.setattr(reconcile_sweep, "load_static_facts", lambda: {})
    monkeypatch.setattr(reconcile_sweep, "_fresh_live_origin", lambda _now, _age: None)
    monkeypatch.setattr(reconcile_sweep, "_boarding_block_end_times", lambda *_args, **_kwargs: [])

    def capture_meeting_blocks(meetings, *, route, driving_to, display_tz=None):
        captured["display_tz"] = display_tz
        return [], [], []

    def capture_fetch_window(self, *, time_min, time_max):
        captured["window_start"] = time_min
        return []

    monkeypatch.setattr(_EmptyFetcher, "fetch_window", capture_fetch_window)

    monkeypatch.setattr(reconcile_sweep, "meeting_desired_blocks", capture_meeting_blocks)
    monkeypatch.setattr(
        reconcile_sweep,
        "build_plan",
        lambda **_kwargs: SimpleNamespace(
            plan=ReconcilePlan(), skipped=(), held=(), origin_sources=()
        ),
    )
    monkeypatch.setattr(
        reconcile_sweep, "finish_sweep", lambda *_args, **_kwargs: {"wake_agent": False, "data": {}}
    )
    return captured


def test_sweep_passes_the_operator_zone_to_the_meeting_planner(monkeypatch, tmp_path):
    zone = OperatorTz(
        tz="America/Chicago", local_now="2026-09-11T08:00:00-05:00", local_date="2026-09-11"
    )
    captured = _stub_live_clients(monkeypatch, lambda: zone, tmp_path)
    assert reconcile_sweep._run_sweep() == {"wake_agent": False, "data": {"origin_sources": []}}
    assert captured["display_tz"] == "America/Chicago"
    # The frozen clock is the one the sweep used for its calendar window.
    assert captured["window_start"] == datetime(2026, 9, 11, 13, 0, tzinfo=UTC)


def test_sweep_passes_none_when_the_reader_has_no_zone(monkeypatch, tmp_path):
    captured = _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    assert reconcile_sweep._run_sweep() == {"wake_agent": False, "data": {"origin_sources": []}}
    assert captured["display_tz"] is None


def test_home_metro_placeholder_routes_meeting_from_current_home(monkeypatch, tmp_path):
    from datetime import timedelta

    import addresses
    import home_address

    real_meeting_blocks = reconcile_sweep.meeting_desired_blocks
    real_build_plan = reconcile_sweep.build_plan
    _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    profile = tmp_path / "user_profile.md"
    profile.write_text(
        "## Addresses\n- schema_version: 2\n"
        "- current_home: 12 Example St\n- home_metro: Nashville, TN\n"
    )
    monkeypatch.setattr(addresses, "profile_path", lambda: profile)
    monkeypatch.setattr(home_address, "read_current_home", lambda: "12 Example St")
    monkeypatch.setattr(state, "read_config", lambda: {})
    monkeypatch.setattr(
        trip_origin,
        "load_travel_schedule",
        lambda: [
            {
                "type": "Trip",
                "summary": "Local placeholder",
                "start": "2026-09-11",
                "end": "2026-09-11",
                "location": "NASHVILLE,  TN",
            }
        ],
    )
    monkeypatch.setattr(
        _EmptyFetcher,
        "fetch_window",
        lambda self, **_: [
            {
                "id": "downtown",
                "summary": "Downtown meeting",
                "location": "42 Example Ave",
                "start": {"dateTime": "2026-09-11T18:00:00Z"},
                "end": {"dateTime": "2026-09-11T19:00:00Z"},
            }
        ],
    )
    monkeypatch.setattr(
        reconcile_sweep,
        "make_route",
        lambda _, **_kwargs: lambda origin, dest: timedelta(minutes=40),
    )
    monkeypatch.setattr(reconcile_sweep, "meeting_desired_blocks", real_meeting_blocks)
    monkeypatch.setattr(reconcile_sweep, "build_plan", real_build_plan)
    plans = []
    monkeypatch.setattr(
        reconcile_sweep,
        "finish_sweep",
        lambda plan, *_args, **_kwargs: plans.append(plan) or {"data": {}},
    )

    reconcile_sweep._run_sweep()
    desired = {create.desired.kind: create.desired for create in plans[0].creates}
    outbound = desired["meeting_outbound"]
    returning = desired["meeting_return"]
    assert outbound.origin == "12 Example St"
    assert outbound.destination == "42 Example Ave"
    assert outbound.baseline_seconds == 2400
    assert outbound.start == datetime(2026, 9, 11, 17, 20, tzinfo=UTC)
    assert returning.origin == "42 Example Ave"
    assert returning.destination == "12 Example St"
    assert returning.end == datetime(2026, 9, 11, 19, 40, tzinfo=UTC)


def _arrival_day_sweep(monkeypatch, tmp_path, *, meeting_start, meeting_end):
    """Run `_run_sweep` over a BNA→DEN trip whose hotel checks in after landing."""
    from datetime import timedelta

    real_meeting_blocks = reconcile_sweep.meeting_desired_blocks
    real_build_plan = reconcile_sweep.build_plan
    _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    monkeypatch.setattr(state, "read_config", lambda: {"home_address": "12 Example St"})
    monkeypatch.setattr(
        trip_origin,
        "load_travel_schedule",
        lambda: [
            {
                "type": "Trip",
                "summary": "Denver",
                "start": "2026-09-12",
                "end": "2026-09-14",
                "location": "Denver, CO",
            },
            {
                "type": "Flight",
                "uid": "tripit-wn1",
                "summary": "WN1 BNA to DEN",
                "start": "2026-09-12T17:20:00Z",
                "end": "2026-09-12T22:10:00Z",
            },
            {
                "type": "Lodging",
                "summary": "Check-in: Late Hotel",
                "start": "2026-09-13T02:00:00Z",
                "location": "Late Hotel Address",
            },
            {
                "type": "Lodging",
                "summary": "Check-out: Late Hotel",
                "start": "2026-09-14T15:00:00Z",
                "location": "Late Hotel Address",
            },
        ],
    )
    monkeypatch.setattr(
        _EmptyFetcher,
        "fetch_window",
        lambda self, **_: [
            {
                "id": "mtg",
                "summary": "Keynote",
                "location": "Conference Center, Denver",
                "start": {"dateTime": meeting_start.isoformat()},
                "end": {"dateTime": meeting_end.isoformat()},
            }
        ],
    )
    monkeypatch.setattr(
        reconcile_sweep,
        "make_route",
        lambda _, **_kwargs: lambda origin, dest: timedelta(minutes=30),
    )
    monkeypatch.setattr(reconcile_sweep, "meeting_desired_blocks", real_meeting_blocks)
    monkeypatch.setattr(reconcile_sweep, "build_plan", real_build_plan)
    plans = []
    monkeypatch.setattr(
        reconcile_sweep,
        "finish_sweep",
        lambda plan, *_args, **_kwargs: plans.append(plan) or {"data": {}},
    )
    reconcile_sweep._run_sweep()
    return [create.desired for create in plans[0].creates]


def test_sweep_arrival_day_meeting_before_check_in_drives_from_and_to_the_hotel(
    monkeypatch, tmp_path
):
    created = _arrival_day_sweep(
        monkeypatch,
        tmp_path,
        meeting_start=datetime(2026, 9, 13, 0, 30, tzinfo=UTC),
        meeting_end=datetime(2026, 9, 13, 1, 30, tzinfo=UTC),
    )
    legs = {b.kind: b for b in created if b.identity == "mtg"}
    assert legs["meeting_outbound"].origin == "Late Hotel Address"
    assert legs["meeting_return"].destination == "Late Hotel Address"
    assert all("Denver, CO" not in (b.origin, b.destination) for b in legs.values())


def test_sweep_ongoing_meeting_after_landing_replaces_the_airport_to_hotel_drive(
    monkeypatch, tmp_path
):
    created = _arrival_day_sweep(
        monkeypatch,
        tmp_path,
        meeting_start=datetime(2026, 9, 12, 20, 30, tzinfo=UTC),
        meeting_end=datetime(2026, 9, 12, 23, 45, tzinfo=UTC),
    )
    arrival = next(b for b in created if b.kind == "airport_arrival")
    assert arrival.destination == "Conference Center, Denver"
    ret = next(b for b in created if b.identity == "mtg" and b.kind == "meeting_return")
    assert ret.destination == "Late Hotel Address"
    assert not any(b.kind == "meeting_outbound" for b in created)


def test_sweep_reads_owned_attendance_before_meeting_planning(monkeypatch, tmp_path):
    from conference_attendance import record_attendance

    real_meeting_blocks = reconcile_sweep.meeting_desired_blocks
    real_build_plan = reconcile_sweep.build_plan
    _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    record_attendance(
        {
            "id": "conference",
            "starts_on": "2026-09-11",
            "ends_on": "2026-09-11",
            "time_zone": "UTC",
            "venue_locations": ["ExampleConf Denver"],
        }
    )
    monkeypatch.setattr(
        _EmptyFetcher,
        "fetch_window",
        lambda self, **_: [
            {
                "id": "native-new-meeting",
                "summary": "Meeting",
                "location": "New Booth - ExampleConf Denver",
                "start": {"dateTime": "2026-09-11T18:00:00Z"},
                "end": {"dateTime": "2026-09-11T19:00:00Z"},
            }
        ],
    )
    calls = []
    monkeypatch.setattr(
        reconcile_sweep, "make_route", lambda _, **_kwargs: lambda o, d: calls.append((o, d))
    )
    monkeypatch.setattr(reconcile_sweep, "meeting_desired_blocks", real_meeting_blocks)
    monkeypatch.setattr(reconcile_sweep, "build_plan", real_build_plan)
    plans = []
    monkeypatch.setattr(
        reconcile_sweep, "finish_sweep", lambda plan, *_a, **_kw: plans.append(plan) or {"data": {}}
    )
    reconcile_sweep._run_sweep()
    assert calls == []
    assert plans[0].creates == ()


def test_unknown_attendance_schema_stops_sweep_before_apply(monkeypatch, tmp_path):
    import pytest
    from conference_attendance import AttendanceError

    _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    path = tmp_path / "conference-attendance.json"
    raw = '{"schema_version":2,"attendances":[]}'
    path.write_text(raw)
    calls = []
    monkeypatch.setattr(reconcile_sweep, "finish_sweep", lambda *_a, **_kw: calls.append("apply"))
    with pytest.raises(AttendanceError):
        reconcile_sweep._run_sweep()
    assert calls == []
    assert path.read_text() == raw


def test_live_sweep_uses_route_distance_and_measured_ev_time_before_single_reconcile(
    monkeypatch, tmp_path
):
    import tesla_planning
    from calendar_apply import build_create_args
    from ev_charging import EnergySample
    from maps_client import TravelTime

    real_meetings = reconcile_sweep.meeting_desired_blocks
    real_build = reconcile_sweep.build_plan
    _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    settings = {
        "schema_version": 1,
        "stop_minutes": 30,
        "reserve_percent": 10,
        "cold_allowance_percent": 0,
        "highway_allowance_percent": 0,
        "default_vehicle": "Car",
        "event_vehicles": {},
        "person_vehicles": {},
        "event_drivers": {},
        "destination_charges": [],
    }
    monkeypatch.setattr(
        state,
        "read_config",
        lambda: {
            "home_address": "12 Example St",
            "ev_planning": settings,
        },
    )
    monkeypatch.setattr(
        _EmptyFetcher,
        "fetch_window",
        lambda self, **_: [
            {
                "id": "long-meeting",
                "summary": "Long meeting",
                "location": "Far venue",
                "start": {"dateTime": "2026-09-11T18:00:00Z"},
                "end": {"dateTime": "2026-09-11T19:00:00Z"},
            }
        ],
    )
    routes = []

    class Maps:
        def travel_time(self, origin, destination):
            routes.append((origin, destination))
            return TravelTime(10800, None, 1, 395000, origin, destination, "google")

    monkeypatch.setattr(maps_client.MapsClient, "from_env", classmethod(lambda cls, **_: Maps()))
    selectors = []
    departures = []

    class Provider:
        def __init__(self, *, now, configured):
            assert configured
            self.now = now

        def vehicle(self, selector):
            selectors.append(selector)
            return "Car"

        def estimate(self, vehicle, distance, departure):
            departures.append((vehicle, distance, departure))
            return EnergySample(vehicle, 75, 0.18, self.now, 90)

        def navigation(self):
            return []

        def unavailable(self):
            raise AssertionError("Synthetic complete provider must not be held")

    monkeypatch.setattr(tesla_planning, "TeslaPlanning", Provider)
    monkeypatch.setattr(reconcile_sweep, "meeting_desired_blocks", real_meetings)
    monkeypatch.setattr(reconcile_sweep, "build_plan", real_build)
    plans = []
    monkeypatch.setattr(
        reconcile_sweep, "finish_sweep", lambda plan, *_a, **_kw: plans.append(plan) or {"data": {}}
    )
    reconcile_sweep._run_sweep()
    assert len(plans) == 1 and len(plans[0].creates) == 2
    blocks = {create.desired.kind: create.desired for create in plans[0].creates}
    out = blocks["meeting_outbound"]
    assert out.start == datetime(2026, 9, 11, 14, 30, tzinfo=UTC)
    assert out.end == datetime(2026, 9, 11, 18, tzinfo=UTC)
    assert out.baseline_seconds == 12600
    assert (
        "1 charging stop, ~30 min" in build_create_args(out, calendar_id="primary")["description"]
    )
    assert selectors == ["Car", "Car"]
    assert len(routes) == 2  # distance and duration share the actual maps results
    assert departures[-1][2] == out.start


def test_invalid_ev_preferences_hold_drive_sweep_before_clients(monkeypatch, tmp_path):
    import pytest

    _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    monkeypatch.setattr(
        state,
        "read_config",
        lambda: {
            "home_address": "Home",
            "ev_planning": {"schema_version": 99},
        },
    )
    calls = []
    monkeypatch.setattr(
        maps_client.MapsClient, "from_env", classmethod(lambda cls, **_: calls.append("maps"))
    )
    with pytest.raises(ValueError, match="schema 1"):
        reconcile_sweep._run_sweep()
    assert calls == []
