"""Charging presentation changes must survive ordinary traffic-jitter tolerance."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name in ("travel-core", "flight-assist", "drive-engine"):
    sys.path.insert(0, str(ROOT / "skills" / name))

from block_codec import parse_block  # noqa: E402
from calendar_apply import apply_plan, build_create_args  # noqa: E402
from reconcile import DesiredBlock, plan_reconcile  # noqa: E402

NOW = datetime(2020, 7, 13, tzinfo=UTC)


def base():
    return DesiredBlock(
        "meeting",
        "meeting_outbound",
        "Drive: Meeting",
        NOW + timedelta(hours=9),
        NOW + timedelta(hours=10),
        "Home",
        "Venue",
        3600,
        NOW + timedelta(hours=10),
    )


def charged():
    value = base()
    return replace(
        value,
        start=value.start - timedelta(minutes=1),
        baseline_seconds=3660,
        charging_note="1 charging stop, ~1 min",
    )


class Calendar:
    def __init__(self):
        self.patches = []

    def patch_event(self, arguments):
        self.patches.append(arguments)


def reconcile(prior, desired):
    event = {"id": "native-event", **build_create_args(prior, calendar_id="primary")}
    parsed = parse_block(event)
    assert parsed is not None
    plan = plan_reconcile([desired], [parsed], now=NOW)
    assert len(plan.updates) == 1 and not plan.creates and not plan.deletes
    calendar = Calendar()
    result = apply_plan(plan, calendar=calendar, now=NOW, creation_clock=lambda: NOW)
    assert result.updated == 1 and len(calendar.patches) == 1
    assert calendar.patches[0]["event_id"] == "native-event"
    return calendar.patches[0]


def test_add_minimum_allowance_to_existing_calendar_block():
    patch = reconcile(base(), charged())
    assert patch["start"]["dateTime"] == (base().start - timedelta(minutes=1)).isoformat()
    assert "1 charging stop, ~1 min" in patch["description"]


def test_remove_minimum_allowance_from_existing_calendar_block():
    patch = reconcile(charged(), base())
    assert patch["start"]["dateTime"] == base().start.isoformat()
    assert patch["description"] == "Home → Venue"


def test_unchanged_ev_presentation_keeps_ordinary_traffic_jitter_noop():
    prior = charged()
    event = {"id": "native-event", **build_create_args(prior, calendar_id="primary")}
    parsed = parse_block(event)
    assert parsed is not None
    desired = replace(prior, start=prior.start - timedelta(seconds=30), baseline_seconds=3690)
    assert plan_reconcile([desired], [parsed], now=NOW).is_noop


def test_unavailable_vehicle_preserves_existing_ev_calendar_block(monkeypatch):
    import tesla_planning
    from reconcile_sweep import _ev_adjuster, build_plan

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, _selector):
            return None

        def estimate(self, *_arguments):
            raise AssertionError("Unavailable vehicle must not obtain another forecast")

        def navigation(self):
            return []

        def unavailable(self):
            pass

    monkeypatch.setattr(tesla_planning, "TeslaPlanning", Provider)
    prior = replace(
        base(),
        start=base().start - timedelta(minutes=30),
        baseline_seconds=5400,
        charging_note="1 charging stop, ~30 min",
    )
    parsed = parse_block({"id": "native-event", **build_create_args(prior, calendar_id="primary")})
    assert parsed is not None
    result = build_plan(
        flight_records=[],
        resolve_airport=lambda _: None,
        meeting_blocks=[base()],
        current_blocks=[parsed],
        route=lambda *_: timedelta(hours=1),
        now=NOW,
        home_address="Home",
        adjust_drives=_ev_adjuster({}, lambda *_: None, {}, NOW),
    )
    assert result.plan.is_noop
    assert [(item.identity, item.kind, item.announce) for item in result.held] == [
        ("meeting", "meeting_outbound", False)
    ]
