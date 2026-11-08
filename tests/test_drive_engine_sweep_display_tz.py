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

import pytest

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

    class Maps:
        def geocode(self, _place):
            raise maps_client.MapsError("ZERO_RESULTS", "Synthetic unknown place")

    monkeypatch.setattr(maps_client.MapsClient, "from_env", classmethod(lambda cls, **_: Maps()))
    monkeypatch.setattr(byair_client.ByAirClient, "from_env", classmethod(lambda cls, **_: None))
    monkeypatch.setattr(google_calendar_client, "GoogleCalendarClient", _EmptyCalendar)
    monkeypatch.setattr(fetch_events, "CalendarFetcher", _EmptyFetcher)

    # Module-level collaborators of the sweep that touch disk or the network.
    monkeypatch.setattr(reconcile_sweep, "load_verdicts", lambda _now: {})
    monkeypatch.setattr(reconcile_sweep, "load_static_facts", lambda: {})
    monkeypatch.setattr(reconcile_sweep, "_fresh_live_origin", lambda _now, _age: None)
    monkeypatch.setattr(reconcile_sweep, "_boarding_block_end_times", lambda *_args, **_kwargs: [])

    def capture_meeting_blocks(meetings, *, route, driving_to, display_tz=None, booked=None):
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
    raw = '{"schema_version":3,"attendances":[]}'
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
    assert out.baseline_seconds == 10800
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


@pytest.mark.parametrize("case", ["busy", "clear", "malformed", "fetch_failure"])
def test_no_home_sweep_protects_busy_events_from_airport_charging_extension(
    monkeypatch, tmp_path, capsys, case
):
    from datetime import timedelta

    import home_address
    import tesla_planning
    from ev_charging import EnergySample
    from reconcile import DesiredBlock

    _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    monkeypatch.setattr(state, "read_config", lambda: {})
    monkeypatch.setattr(home_address, "read_current_home", lambda: None)
    now = _FrozenDatetime.now(UTC)

    def fetch(_self, **_kwargs):
        if case == "fetch_failure":
            raise google_calendar_client.GoogleCalendarError("synthetic event fetch unavailable")
        if case == "clear":
            return []
        event = {
            "id": "busy",
            "summary": "Appointment",
            "start": {"dateTime": (now + timedelta(hours=3, minutes=10)).isoformat()},
            "end": {"dateTime": (now + timedelta(hours=3, minutes=20)).isoformat()},
        }
        if case == "malformed":
            event.pop("end")
        return [event]

    monkeypatch.setattr(_EmptyFetcher, "fetch_window", fetch)

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, _selector):
            assert case in {"busy", "clear"}, "No provider reads with unknown protected windows"
            return "Car"

        def estimate(self, *_args):
            return EnergySample("Car", 75, 0.18, now, 90)

        def navigation(self):
            return []

        def unavailable(self):
            pass

    monkeypatch.setattr(tesla_planning, "TeslaPlanning", Provider)
    dep = DesiredBlock(
        "flight",
        "airport_departure",
        "Drive",
        now + timedelta(minutes=30),
        now + timedelta(hours=1),
        "Hotel",
        "AAA",
        1800,
        now + timedelta(hours=1),
    )
    arr = DesiredBlock(
        "flight",
        "airport_arrival",
        "Drive",
        now + timedelta(hours=2),
        now + timedelta(hours=3),
        "BBB",
        "Hotel",
        3600,
        now + timedelta(hours=2),
    )
    actual_adjuster = reconcile_sweep._ev_adjuster

    def adjusted(config, route, distances, clock, busy_windows=()):
        return actual_adjuster(
            config, route, {("Hotel", "AAA"): 10, ("BBB", "Hotel"): 395}, clock, busy_windows
        )

    monkeypatch.setattr(reconcile_sweep, "_ev_adjuster", adjusted)
    seen = []

    def build(**arguments):
        result, _ = arguments["adjust_drives"]([dep, arr], [], [])
        seen.append(result)
        return SimpleNamespace(plan=ReconcilePlan(), skipped=(), held=(), origin_sources=())

    monkeypatch.setattr(reconcile_sweep, "build_plan", build)
    reconcile_sweep._run_sweep()
    assert len(seen) == 1
    if case == "clear":
        assert seen[0][1].end == arr.end + timedelta(minutes=30)
    else:
        assert seen[0][1] is arr
    if case in {"malformed", "fetch_failure"}:
        assert "protected Calendar windows unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["window", "no-window", "legacy"])
def test_attendance_v2_full_sweep_commute_outside_and_late_airport(monkeypatch, tmp_path, mode):
    """Real sweep/plan/apply pipeline with fixed fake clients; no live Calendar."""
    from datetime import timedelta

    from block_codec import parse_block
    from calendar_apply import apply_plan
    from conference_attendance import record_attendance

    real_meetings, real_build, real_finish = (
        reconcile_sweep.meeting_desired_blocks,
        reconcile_sweep.build_plan,
        reconcile_sweep.finish_sweep,
    )
    _stub_live_clients(monkeypatch, lambda: None, tmp_path)
    monkeypatch.setattr(trip_origin, "home_airports", lambda: frozenset({"BNA"}))
    intake = {
        "schema_version": 2,
        "id": "synthetic-occurrence",
        "starts_on": "2026-09-11",
        "ends_on": "2026-09-11",
        "time_zone": "UTC",
        "venue_locations": ["SyntheticConf"],
        "venue_location": "100 Venue Street",
        "days": [
            {
                "date": "2026-09-11",
                "onsite_from": "14:00",
                "onsite_to": "17:00",
                "source": "owner",
                "source_ref": None,
            }
        ],
    }
    if mode == "no-window":
        intake["days"] = []
    elif mode == "legacy":
        for key in ("schema_version", "venue_location", "days"):
            intake.pop(key)
    record_attendance(intake)
    monkeypatch.setattr(
        trip_origin,
        "load_travel_schedule",
        lambda: [
            {
                "type": "Trip",
                "summary": "Synthetic trip",
                "location": "Example City",
                "start": "2026-09-10",
                "end": "2026-09-12",
            },
            {
                "type": "Lodging",
                "summary": "Check-in: Hotel",
                "location": "200 Hotel Street",
                "start": "2026-09-10T15:00:00Z",
            },
            {
                "type": "Lodging",
                "summary": "Check-out: Hotel",
                "location": "200 Hotel Street",
                "start": "2026-09-11T15:00:00Z",
            },
            {
                "type": "Flight",
                "summary": "AA1 LGA to BNA",
                "uid": "native-synthetic-segment",
                "start": "2026-09-11T23:00:00Z",
                "end": "2026-09-12T01:00:00Z",
            },
        ],
    )
    events = [
        {
            "id": "native-booth",
            "summary": "Meeting",
            "location": "Booth - SyntheticConf",
            "start": {"dateTime": "2026-09-11T14:00:00Z"},
            "end": {"dateTime": "2026-09-11T14:20:00Z"},
        },
        {
            "id": "native-outside",
            "summary": "Outside appointment",
            "location": "300 Outside Street",
            "start": {"dateTime": "2026-09-11T16:00:00Z"},
            "end": {"dateTime": "2026-09-11T16:30:00Z"},
        },
    ]
    if mode == "window":
        events[0]["location"] = "Room 52A"

        class CampusMaps:
            def travel_time(self, origin, destination):
                meters = {"Room 52A": 200, "300 Outside Street": 2200}[destination]
                return type("TT", (), {"distance_meters": meters})()

        monkeypatch.setattr(
            maps_client.MapsClient, "from_env", classmethod(lambda cls, **_: CampusMaps())
        )
    monkeypatch.setattr(_EmptyFetcher, "fetch_window", lambda self, **_: events)
    monkeypatch.setattr(
        reconcile_sweep, "make_route", lambda *_a, **_k: lambda o, d: timedelta(minutes=30)
    )
    monkeypatch.setattr(reconcile_sweep, "meeting_desired_blocks", real_meetings)
    monkeypatch.setattr(reconcile_sweep, "build_plan", real_build)

    def observe_finish(plan, skipped, **kwargs):
        def apply_frozen(plan, **options):
            return apply_plan(plan, creation_clock=lambda: _FrozenDatetime.now(UTC), **options)

        return real_finish(plan, skipped, apply=apply_frozen, **kwargs)

    monkeypatch.setattr(reconcile_sweep, "finish_sweep", observe_finish)

    class Calendar:
        rows: list[dict] = []
        mutations: list[str] = []

        def find_events(self, _arguments):
            return {"items": list(self.rows)}

        def create_event(self, args):
            native = {**args, "id": f"created-{len(self.rows)}"}
            self.rows.append(native)
            self.mutations.append("create")
            return native

        def update_event(self, _arguments):
            raise AssertionError("unchanged second sweep must not update")

        def delete_event(self, _arguments):
            raise AssertionError("unchanged second sweep must not delete")

    monkeypatch.setattr(google_calendar_client, "GoogleCalendarClient", Calendar)
    first = reconcile_sweep._run_sweep()
    blocks = [parse_block(e) for e in Calendar.rows]
    assert all(b is not None for b in blocks)
    by_kind = {}
    for b in blocks:
        assert b is not None
        by_kind.setdefault(b.kind, []).append(b)
    assert "attendance_return" not in by_kind
    if mode == "window":
        assert len(by_kind["attendance_outbound"]) == 1
        assert by_kind["attendance_outbound"][0].origin == "200 Hotel Street"
        assert by_kind["attendance_outbound"][0].destination == "100 Venue Street"
        assert by_kind["meeting_outbound"][0].origin == "100 Venue Street"
        assert by_kind["airport_departure"][0].origin == "100 Venue Street"
        assert by_kind["airport_departure"][0].anchor > datetime(2026, 9, 11, 17, tzinfo=UTC)
        assert not any(b is not None and b.destination == "200 Hotel Street" for b in blocks)
        assert "Outside appointment" in first["data"]["message"]
    elif mode == "legacy":
        assert "attendance_outbound" not in by_kind
        assert by_kind["meeting_outbound"][0].origin == "200 Hotel Street"
        assert by_kind["airport_departure"][0].origin == "200 Hotel Street"
        assert all(b is not None and b.identity != "native-booth" for b in blocks)
    else:
        assert "attendance_outbound" not in by_kind and "airport_departure" not in by_kind
        assert by_kind["meeting_outbound"][0].identity == "native-booth"
        assert by_kind["meeting_outbound"][0].origin == "200 Hotel Street"
        assert all(b is not None and b.identity != "native-outside" for b in blocks)
    before = list(Calendar.mutations)
    second = reconcile_sweep._run_sweep()
    assert Calendar.mutations == before
    assert second["data"]["message"] is None
