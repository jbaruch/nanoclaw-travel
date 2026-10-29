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


@pytest.mark.parametrize("case", ["middle", "far_return", "far_arrive_by"])
def test_actual_route_failed_middle_enters_engine_ev_continuity(monkeypatch, case):
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
    after = NOW + (timedelta(hours=4) if case == "middle" else timedelta(days=30))
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
                direction="outbound", origin="Home", destination="Middle venue", arrive_by=after
            )
            if case == "far_arrive_by"
            else TransitLeg(
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
    if case == "middle":
        assert planned["later"] is later
    else:
        assert planned["later"].charging_note is not None


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


@pytest.mark.parametrize("adding", [True, False])
def test_material_return_allowance_notice_changes_arrival_not_departure(adding):
    from reconcile_sweep import build_sweep_payload

    initial = replace(
        base(), kind="meeting_return", start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2)
    )
    extended = replace(
        initial, end=initial.end + timedelta(minutes=30), charging_note="1 charging stop, ~30 min"
    )
    prior, desired = (initial, extended) if adding else (extended, initial)
    current = parse_block({"id": "return", **build_create_args(prior, calendar_id="primary")})
    assert current is not None
    result = apply_plan(plan_reconcile([desired], [current], now=NOW), calendar=Calendar(), now=NOW)
    message = build_sweep_payload(result, [])["data"]["message"]
    assert "arrive 30 min " + ("later" if adding else "sooner") in message
    assert "leave" not in message
    assert desired.start == prior.start


@pytest.mark.parametrize("native_bounds", [True, False])
def test_unavailable_middle_retained_native_allowance_fences_preceding_extension(
    monkeypatch, native_bounds
):
    import tesla_planning
    from ev_charging import EnergySample
    from reconcile_sweep import _ev_adjuster

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, selector):
            return None if selector == "Unavailable" else "Car"

        def estimate(self, *_args):
            return EnergySample("Car", 75, 0.18, NOW, 90)

        def navigation(self):
            return []

        def unavailable(self):
            pass

    monkeypatch.setattr(tesla_planning, "TeslaPlanning", Provider)
    outbound = replace(
        base(),
        identity="first",
        start=NOW + timedelta(minutes=30),
        end=NOW + timedelta(hours=1),
        origin="Home",
        destination="First",
        baseline_seconds=1800,
    )
    returned = replace(
        base(),
        identity="first",
        kind="meeting_return",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=3),
        origin="First",
        destination="Home",
    )
    middle = replace(
        base(), identity="middle", start=NOW + timedelta(hours=4), end=NOW + timedelta(hours=5)
    )
    retained = replace(
        middle, start=NOW + timedelta(hours=3, minutes=10), charging_note="1 charging stop, ~50 min"
    )
    event = {"id": "retained-middle", **build_create_args(retained, calendar_id="primary")}
    if not native_bounds:
        event.pop("start")
        event.pop("end")
    current = parse_block(event)
    assert current is not None
    preferences = {
        "schema_version": 1,
        "stop_minutes": 30,
        "reserve_percent": 10,
        "cold_allowance_percent": 0,
        "highway_allowance_percent": 0,
        "default_vehicle": "Car",
        "event_vehicles": {"middle": "Unavailable"},
        "person_vehicles": {},
        "event_drivers": {},
        "destination_charges": [],
    }
    result, held = _ev_adjuster(
        {"ev_planning": preferences},
        lambda *_: None,
        {("Home", "First"): 10, ("First", "Home"): 395},
        NOW,
    )([outbound, returned, middle], [], [current])
    assert result[1] is returned
    assert [(item.identity, item.kind) for item in held] == [("middle", "meeting_outbound")]


def test_roundtrip_material_notices_keep_departure_and_arrival_polarity_separate():
    from reconcile_sweep import build_sweep_payload

    outbound = replace(
        base(),
        start=NOW + timedelta(minutes=30),
        end=NOW + timedelta(hours=1),
        baseline_seconds=1800,
    )
    returned = replace(
        outbound,
        kind="meeting_return",
        start=NOW + timedelta(hours=1, minutes=10),
        end=NOW + timedelta(hours=1, minutes=40),
    )
    changed_out = replace(
        outbound,
        start=outbound.start - timedelta(minutes=30),
        charging_note="1 charging stop, ~30 min",
    )
    changed_back = replace(
        returned, end=returned.end + timedelta(minutes=30), charging_note="1 charging stop, ~30 min"
    )
    current = [
        parse_block({"id": str(i), **build_create_args(block, calendar_id="primary")})
        for i, block in enumerate([outbound, returned])
    ]
    assert all(item is not None for item in current)
    plan = plan_reconcile(
        [changed_out, changed_back], [item for item in current if item is not None], now=NOW
    )
    payload = build_sweep_payload(apply_plan(plan, calendar=Calendar(), now=NOW), [])["data"]
    assert len(payload["material_updates"]) == 2
    assert "leave 30 min sooner" in payload["message"]
    assert "arrive 30 min later" in payload["message"]


def test_airport_arrival_material_notice_uses_depart_after_side_label():
    from reconcile_sweep import build_sweep_payload

    prior = replace(
        base(), kind="airport_arrival", start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2)
    )
    changed = replace(
        prior, end=prior.end + timedelta(minutes=30), charging_note="1 charging stop, ~30 min"
    )
    current = parse_block({"id": "arrival", **build_create_args(prior, calendar_id="primary")})
    assert current is not None
    payload = build_sweep_payload(
        apply_plan(plan_reconcile([changed], [current], now=NOW), calendar=Calendar(), now=NOW), []
    )["data"]
    assert payload["material_updates"][0]["timing"] == "arrival"
    assert "arrive 30 min later" in payload["message"]
    assert "Outbound" not in payload["message"]


def test_leg_bound_reports_unavailable_before_adapter_provider_reads(monkeypatch, capsys):
    import tesla_planning
    from ev_charging import MAX_LEGS, adjust_charging
    from reconcile_sweep import _ev_adjuster

    blocks = [replace(base(), identity=str(index)) for index in range(MAX_LEGS + 1)]
    notices = []
    result = adjust_charging(
        blocks,
        vehicles={},
        distance_km=lambda _: None,
        estimate=lambda *_: None,
        now=NOW,
        unavailable=notices.append,
    )
    assert all(a is b for a, b in zip(result, blocks, strict=True))
    assert notices == [frozenset((block.identity, block.kind) for block in blocks)]

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, _selector):
            raise AssertionError("No provider reads past planning bound")

        def estimate(self, *_args):
            raise AssertionError("No forecast past planning bound")

        def navigation(self):
            return []

        def unavailable(self):
            notices.append("fixed diagnostic")

    monkeypatch.setattr(tesla_planning, "TeslaPlanning", Provider)
    adjusted, held = _ev_adjuster({}, lambda *_: None, {}, NOW)(blocks, [], [])
    assert not held and len(adjusted) == len(blocks)
    assert "leg limit exceeded" in capsys.readouterr().err


def test_bound_hold_preserves_existing_ev_allowance_and_exact_limit_is_accepted(monkeypatch):
    import tesla_planning
    from ev_charging import MAX_LEGS, adjust_charging
    from reconcile_sweep import _ev_adjuster

    blocks = [replace(base(), identity=str(i)) for i in range(MAX_LEGS + 1)]
    notified = []
    assert (
        adjust_charging(
            blocks[:MAX_LEGS],
            vehicles={},
            distance_km=lambda _: None,
            estimate=lambda *_: None,
            now=NOW,
            unavailable=notified.append,
        )
        == blocks[:MAX_LEGS]
    )
    assert not notified

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, _selector):
            raise AssertionError("No native read past bound")

        def estimate(self, *_args):
            raise AssertionError("No forecast past bound")

        def navigation(self):
            return []

        def unavailable(self):
            pass

    monkeypatch.setattr(tesla_planning, "TeslaPlanning", Provider)
    existing = replace(
        blocks[0],
        charging_note="1 charging stop, ~30 min",
        start=blocks[0].start - timedelta(minutes=30),
    )
    current = parse_block({"id": "retain", **build_create_args(existing, calendar_id="primary")})
    assert current is not None
    adjusted, held = _ev_adjuster({}, lambda *_: None, {}, NOW)(blocks, [], [current])
    assert len(adjusted) == MAX_LEGS
    assert [(item.identity, item.kind, item.announce) for item in held] == [
        ("0", "meeting_outbound", False)
    ]


@pytest.mark.parametrize("retained", [False, True])
def test_missing_outbound_later_return_fences_preceding_extension(monkeypatch, retained):
    import tesla_planning
    from ev_charging import EnergySample
    from reconcile_sweep import _ev_adjuster

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
    outbound = replace(
        base(),
        identity="first",
        start=NOW + timedelta(minutes=30),
        end=NOW + timedelta(hours=1),
        origin="Home",
        destination="First",
        baseline_seconds=1800,
    )
    returned = replace(
        base(),
        identity="first",
        kind="meeting_return",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=3),
        origin="First",
        destination="Home",
        anchor=NOW + timedelta(hours=2),
    )
    actual_start = NOW + timedelta(hours=3, minutes=10)
    desired_start = NOW + timedelta(hours=4) if retained else actual_start
    later = replace(
        base(),
        identity="later",
        kind="lodging_return",
        start=desired_start,
        end=desired_start + timedelta(hours=1),
        anchor=desired_start,
    )
    current = []
    if retained:
        prior = replace(
            later,
            start=actual_start,
            end=actual_start + timedelta(minutes=90),
            anchor=actual_start,
            charging_note="1 charging stop, ~30 min",
        )
        parsed = parse_block(
            {"id": "old-return", **build_create_args(prior, calendar_id="primary")}
        )
        assert parsed is not None
        current = [parsed]
    adjusted, held = _ev_adjuster(
        {}, lambda *_: None, {("Home", "First"): 10, ("First", "Home"): 395}, NOW
    )([outbound, returned, later], [], current)
    assert adjusted[1] is returned
    assert returned.charging_note is None
    if retained:
        assert [(entry.identity, entry.kind) for entry in held] == [("later", "lodging_return")]
    else:
        assert adjusted[2] is later and not held
