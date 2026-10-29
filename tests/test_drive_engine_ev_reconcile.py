"""Charging presentation changes must survive ordinary traffic-jitter tolerance."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
        baseline_seconds=3600,
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
    desired = replace(prior, start=prior.start - timedelta(seconds=30), baseline_seconds=3630)
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
        baseline_seconds=3600,
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


def test_ev_plan_persists_route_only_baseline_while_calendar_and_notice_include_stops():
    from ev_charging import EnergySample, PlanningOptions, adjust_charging

    prior = replace(base(), start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2))
    desired = adjust_charging(
        [prior],
        vehicles={(prior.identity, prior.kind): "Car"},
        distance_km=lambda _: 395,
        estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
        now=NOW,
        options=PlanningOptions(cold_fraction=0, highway_fraction=0),
    )[0]
    assert desired.baseline_seconds == prior.baseline_seconds == 3600
    event = {"id": "native-event", **build_create_args(prior, calendar_id="primary")}
    parsed = parse_block(event)
    assert parsed is not None
    plan = plan_reconcile([desired], [parsed], now=NOW)
    calendar = Calendar()
    result = apply_plan(plan, calendar=calendar, now=NOW)
    assert result.updated == 1
    assert calendar.patches[0]["extendedProperties"]["private"]["dengine_b"] == "3600"
    assert result.material_updates[0]["charging"] == "Outbound: 1 charging stop, ~30 min"


def test_actual_route_failed_middle_enters_engine_ev_continuity(monkeypatch):
    import tesla_planning
    from ev_charging import EnergySample
    from meeting_source import meeting_desired_blocks
    from reconcile_sweep import _ev_adjuster, build_plan
    from scan import MeetingClass, TransitLeg

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, _selector):
            return "Car"

        def estimate(self, *_args):
            return EnergySample("Car", 75, 0.18, NOW, 90)

        def navigation(self):
            return []

        def unavailable(self):
            pass

    monkeypatch.setattr(tesla_planning, "TeslaPlanning", Provider)
    first = replace(
        base(), identity="first", start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2)
    )
    later = replace(
        base(), identity="later", start=NOW + timedelta(hours=8), end=NOW + timedelta(hours=9)
    )
    after = NOW + timedelta(hours=4)
    middle = MeetingClass(
        meeting_id="middle",
        summary="Middle",
        bucket="needs_decision",
        reason="standalone",
        location="Middle venue",
        start=after - timedelta(hours=1),
        end=after,
        legs=(
            TransitLeg(
                direction="return", origin="Middle venue", destination="Home", depart_after=after
            ),
        ),
    )
    blocks, _, held = meeting_desired_blocks([middle], route=lambda *_: None)
    assert not blocks and len(held) == 1
    result = build_plan(
        flight_records=[],
        resolve_airport=lambda _: None,
        meeting_blocks=[first, later],
        held=held,
        current_blocks=[],
        route=lambda *_: None,
        now=NOW,
        home_address="Home",
        adjust_drives=_ev_adjuster({}, lambda *_: None, {("Home", "Venue"): 395}, NOW),
    )
    planned = {entry.desired.identity: entry.desired for entry in result.plan.creates}
    assert planned["first"].charging_note is not None
    assert planned["later"] is later


def test_navigation_span_change_updates_without_redefining_route_baseline():
    prior = replace(base(), kind="meeting_return", charging_note="Car navigation ETA")
    desired = replace(prior, end=prior.end + timedelta(minutes=3))
    patch = reconcile(prior, desired)
    assert patch["extendedProperties"]["private"]["dengine_b"] == "3600"
    assert patch["end"]["dateTime"] == desired.end.isoformat()


@pytest.mark.parametrize("gap_start", [None, NOW - timedelta(minutes=1)])
def test_held_energy_gap_requires_independent_exact_active_route(gap_start):
    from ev_charging import EnergyGap, EnergySample, Navigation, PlanningOptions, adjust_charging

    block = replace(base(), start=NOW + timedelta(minutes=2))

    def adjusted(navigation=()):
        return adjust_charging(
            [block],
            vehicles={(block.identity, block.kind): "Car"},
            distance_km=lambda _: 395,
            estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
            now=NOW,
            options=PlanningOptions(cold_fraction=0, highway_fraction=0),
            energy_gaps=[EnergyGap(gap_start, "Car")],
            navigation=navigation,
        )[0]

    assert adjusted() is block
    wrong = Navigation("Other", "Venue", NOW, 25, 40)
    assert adjusted([wrong]) is block
    exact = replace(wrong, vehicle="Car")
    assert adjusted([exact]).charging_note == "Car navigation ETA"


def test_ev_prefix_inside_a_route_endpoint_does_not_hold_legacy_update(monkeypatch):
    import tesla_planning
    from reconcile_sweep import _ev_adjuster, build_plan

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, _selector):
            return None

        def estimate(self, *_args):
            raise AssertionError("Unavailable provider")

        def navigation(self):
            return []

        def unavailable(self):
            pass

    monkeypatch.setattr(tesla_planning, "TeslaPlanning", Provider)
    prior = replace(base(), origin="Home\nEV planning: source endpoint")
    desired = replace(
        prior,
        anchor=prior.anchor + timedelta(hours=1),
        start=prior.start + timedelta(hours=1),
        end=prior.end + timedelta(hours=1),
    )
    current = parse_block({"id": "owned-legacy", **build_create_args(prior, calendar_id="primary")})
    assert current is not None
    result = build_plan(
        flight_records=[],
        resolve_airport=lambda _: None,
        meeting_blocks=[desired],
        current_blocks=[current],
        route=lambda *_: None,
        now=NOW,
        home_address="Home",
        adjust_drives=_ev_adjuster({}, lambda *_: None, {}, NOW),
    )
    assert not result.held
    assert len(result.plan.updates) == 1
    assert result.plan.updates[0].desired is desired


@pytest.mark.parametrize(
    "origin,destination",
    [("Home\nEV planning: endpoint", "Venue"), ("Home", "Venue\nEV planning: endpoint")],
)
def test_ev_suffix_parser_preserves_genuine_allowance_after_colliding_route(origin, destination):
    from block_codec import human_description, parse_ev_note

    route = human_description(origin, destination)
    assert parse_ev_note(route, origin, destination) is None
    note = "1 charging stop, ~30 min"
    assert parse_ev_note(human_description(origin, destination, note), origin, destination) == note
    assert parse_ev_note(route + "\nEV planning: ", origin, destination) is None
    assert parse_ev_note(route + "\nEV planning: " + note + "\nextra", origin, destination) is None
