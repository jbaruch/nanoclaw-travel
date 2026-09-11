"""Seam test: `_run_sweep` hands the operator's zone to the meeting planner (#301).

`meeting_desired_blocks(display_tz=...)` and the apply render are unit-tested in
their own files, but the one production line that reads the operator zone and
passes it on lives inside `_run_sweep`, which builds its live clients inline.
Copilot on #308 asked for that wiring to be pinned, deferred here (#311).

Every live client `_run_sweep` constructs is stubbed at its source module, so
the sweep runs end to end over an empty itinerary and an empty calendar. The
test captures the `display_tz` the meeting planner receives for an available
reader result and for `None`. Nothing asserts on the clock the sweep reads.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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


class _EmptyCalendar:
    def find_events(self, _arguments):
        return {"items": []}


class _EmptyFetcher:
    def fetch_window(self, *, time_min, time_max):
        return []


def _stub_live_clients(monkeypatch, reader) -> dict:
    """Stub every live dependency of `_run_sweep`; return the capture dict."""
    captured: dict = {}

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
        return [], []

    monkeypatch.setattr(reconcile_sweep, "meeting_desired_blocks", capture_meeting_blocks)
    monkeypatch.setattr(
        reconcile_sweep,
        "build_plan",
        lambda **_kwargs: SimpleNamespace(plan=ReconcilePlan(), skipped=()),
    )
    monkeypatch.setattr(
        reconcile_sweep, "finish_sweep", lambda *_args, **_kwargs: {"wake_agent": False}
    )
    return captured


def test_sweep_passes_the_operator_zone_to_the_meeting_planner(monkeypatch):
    zone = OperatorTz(
        tz="America/Chicago", local_now="2026-09-11T08:00:00-05:00", local_date="2026-09-11"
    )
    captured = _stub_live_clients(monkeypatch, lambda: zone)
    assert reconcile_sweep._run_sweep() == {"wake_agent": False}
    assert captured["display_tz"] == "America/Chicago"


def test_sweep_passes_none_when_the_reader_has_no_zone(monkeypatch):
    captured = _stub_live_clients(monkeypatch, lambda: None)
    assert reconcile_sweep._run_sweep() == {"wake_agent": False}
    assert captured == {"display_tz": None}
