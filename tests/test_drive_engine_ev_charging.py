"""Measured EV energy, cumulative legs and conservative legacy fallback."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "drive-engine"))

from ev_charging import (  # noqa: E402
    DestinationCharge,
    EnergySample,
    Navigation,
    PlanningOptions,
    adjust_charging,
)
from reconcile import DesiredBlock  # noqa: E402

NOW = datetime(2026, 10, 29, 0, tzinfo=UTC)


def leg(kind="meeting_outbound", *, origin="Home", destination="Venue", hour=9):
    start = NOW + timedelta(hours=hour)
    return DesiredBlock(
        identity="meeting",
        kind=kind,
        summary="Drive: meeting",
        start=start,
        end=start + timedelta(hours=3),
        origin=origin,
        destination=destination,
        baseline_seconds=10800,
        anchor=start if kind.endswith("return") else start + timedelta(hours=3),
    )


def run(blocks, *, km=395, sample=None, charges=(), options=None, vehicles=None):
    values = sample or EnergySample("Car", 75, 0.18, NOW - timedelta(days=1), 90)
    return adjust_charging(
        blocks,
        vehicles=vehicles
        if vehicles is not None
        else {(block.identity, block.kind): "Car" for block in blocks},
        distance_km=lambda _: km,
        estimate=lambda *_: values,
        now=NOW,
        options=options or PlanningOptions(cold_fraction=0, highway_fraction=0),
        destination_charges=charges,
    )


def test_round_trip_consumes_outbound_energy_and_adds_return_stop():
    out = leg()
    back = leg("meeting_return", origin="Venue", destination="Home", hour=15)
    result = run([out, back], km=200)
    assert result[0] is out
    assert result[1].start == back.start
    assert result[1].end == back.end + timedelta(minutes=30)
    assert result[1].baseline_seconds == back.baseline_seconds
    assert result[1].charging_note == "1 charging stop, ~30 min"


def test_long_outbound_moves_leave_by_earlier_without_moving_arrival():
    out = leg()
    result = run([out])[0]
    assert result.start == out.start - timedelta(minutes=30)
    assert result.end == out.end
    assert result.anchor == out.anchor


def test_short_unchanged_objects_and_serialized_calendar_body():
    out = leg()
    assert run([out], km=10) == [out]
    assert run([out], km=10)[0] is out


def test_grounded_destination_energy_splits_two_one_way_checks():
    out = leg()
    back = leg("meeting_return", origin="Venue", destination="Home", hour=39)
    charge = DestinationCharge("Car", "Venue", NOW + timedelta(hours=30), 50)
    assert run([out, back], km=200, charges=[charge]) == [out, back]
    assert run([out, back], km=200)[1] != back


@pytest.mark.parametrize(
    "change",
    [
        {"vehicle": "Other"},
        {"destination": "Other"},
        {"available_at": NOW + timedelta(hours=50)},
        {"available_at": NOW + timedelta(hours=5)},
        {"energy_kwh": float("nan")},
    ],
)
def test_unbound_charging_cannot_reset_return_energy(change):
    out = leg()
    back = leg("meeting_return", origin="Venue", destination="Home", hour=39)
    charge = DestinationCharge("Car", "Venue", NOW + timedelta(hours=30), 50)
    assert run([out, back], km=200, charges=[replace(charge, **change)])[1] != back


@pytest.mark.parametrize(
    "change",
    [
        {"vehicle": "Other"},
        {"capacity_kwh": 0},
        {"measured_kwh_per_km": float("nan")},
        {"departure_percent": None},
        {"departure_percent": True},
        {"measured_at": NOW + timedelta(seconds=1)},
        {"measured_at": NOW - timedelta(days=91)},
    ],
)
def test_invalid_or_stale_sample_preserves_entire_chain(change):
    out = leg()
    sample = EnergySample("Car", 75, 0.18, NOW - timedelta(days=1), 90)
    assert run([out], sample=replace(sample, **change))[0] is out


def test_unknown_estimate_is_exact_legacy_fallback():
    out = leg()
    result = adjust_charging(
        [out],
        vehicles={(out.identity, out.kind): "Car"},
        distance_km=lambda _: 395,
        estimate=lambda *_: None,
        now=NOW,
    )
    assert result[0] is out


@pytest.mark.parametrize("km", [None, -1, True, float("inf")])
def test_unknown_distance_keeps_original(km):
    out = leg()
    assert run([out], km=km)[0] is out


def test_disconnected_route_does_not_reset_the_same_vehicles_energy_forecast():
    out = leg()
    back = leg("meeting_return", origin="Other city", destination="Home", hour=15)
    result = run([out, back], km=200)
    assert result[0] is out
    assert result[1].end == back.end + timedelta(minutes=30)


def test_far_future_airport_leg_does_not_disable_near_long_meeting_drive():
    out = leg()
    airport = replace(
        leg("airport_departure", origin="Home", destination="Airport", hour=24 * 30),
        identity="flight",
    )
    result = run([out, airport])
    assert result[0].start == out.start - timedelta(minutes=30)
    assert result[1] is airport


def test_disconnected_airport_leg_does_not_disable_first_grounded_drive():
    out = leg()
    airport = replace(
        leg("airport_arrival", origin="Far airport", destination="Hotel", hour=24),
        identity="flight",
    )
    result = run([out, airport])
    assert result[0].start == out.start - timedelta(minutes=30)


def test_completed_outbound_cannot_rebase_a_future_return_to_a_full_forecast():
    out = replace(leg(), start=NOW - timedelta(hours=4), end=NOW - timedelta(hours=1))
    back = leg("meeting_return", origin="Venue", destination="Home", hour=15)
    result = run([out, back], km=395)
    assert result[0] is out and result[1] is back


def test_unresolved_outbound_cannot_be_omitted_before_default_vehicle_return():
    out = leg()
    back = leg("meeting_return", origin="Venue", destination="Home", hour=15)
    result = run([out, back], km=395, vehicles={(back.identity, back.kind): "Car"})
    assert result[0] is out and result[1] is back


def test_other_vehicles_outbound_is_not_cumulative_provenance_for_return():
    out = leg()
    back = leg("meeting_return", origin="Venue", destination="Home", hour=15)
    result = run(
        [out, back],
        km=395,
        vehicles={
            (out.identity, out.kind): "Other",
            (back.identity, back.kind): "Car",
        },
    )
    assert result[1] is back


def test_missing_outbound_holds_return_without_erasing_independent_prior_drive():
    earlier = leg()
    back = replace(
        leg("meeting_return", origin="Other venue", destination="Home", hour=15),
        identity="unknown-outbound",
    )
    result = run([earlier, back])
    assert result[0].start == earlier.start - timedelta(minutes=30)
    assert result[1] is back


def test_no_vehicle_selection_never_guesses():
    out = leg()
    assert run([out], vehicles={})[0] is out


def test_custom_duration_reserve_and_explicit_highway_allowance():
    out = leg()
    opts = PlanningOptions(
        stop_minutes=45, reserve_percent=30, cold_fraction=0, highway_fraction=0.1
    )
    result = run([out], km=300, options=opts)[0]
    assert result.start == out.start - timedelta(minutes=45)
    assert result.charging_note == "1 charging stop, ~45 min"


def test_forecasts_earlier_actual_departure_instead_of_later_charge():
    out = leg()
    requested = []

    def estimate(_vehicle, _distance, departure):
        requested.append(departure)
        return EnergySample("Car", 75, 0.18, NOW, 90)

    result = adjust_charging(
        [out],
        vehicles={(out.identity, out.kind): "Car"},
        distance_km=lambda _: 395,
        estimate=estimate,
        now=NOW,
        options=PlanningOptions(cold_fraction=0, highway_fraction=0),
    )
    assert requested == [out.start, result[0].start]


def test_stop_time_that_conflicts_with_prior_leg_preserves_both_originals():
    out = leg()
    back = leg("meeting_return", origin="Venue", destination="Home", hour=12)
    # Return adds time at its end, so add a third arrive-by drive with no slack.
    third = replace(leg(hour=15), identity="other", destination="Other venue")
    assert run([out, back, third], km=200) == [out, back, third]


def test_current_car_navigation_replans_exact_destination_and_vehicle():
    out = leg(hour=0)  # native route at the actual departure, not tomorrow's route
    nav = Navigation("Car", "Venue", NOW, 210, 15)
    result = adjust_charging(
        [out],
        vehicles={(out.identity, out.kind): "Car"},
        distance_km=lambda _: 395,
        estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
        now=NOW,
        navigation=[nav],
    )
    # An arrive-by block cannot move its start into the past; keep legacy.
    assert result[0] is out
    back = leg("meeting_return", origin="Home", destination="Venue", hour=0)
    result = adjust_charging(
        [back],
        vehicles={(back.identity, back.kind): "Car"},
        distance_km=lambda _: 395,
        estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
        now=NOW,
        navigation=[nav],
    )
    assert result[0].end == NOW + timedelta(minutes=210)
    assert result[0].charging_note == "Car navigation ETA"


@pytest.mark.parametrize(
    "change",
    [
        {"vehicle": "Other"},
        {"destination": "Other"},
        {"observed_at": NOW - timedelta(minutes=6)},
        {"observed_at": NOW + timedelta(seconds=1)},
        {"minutes_to_arrival": float("nan")},
        {"arrival_percent": None},
    ],
)
def test_navigation_from_another_route_or_stale_cache_is_not_used(change):
    out = leg(hour=0)
    nav = Navigation("Car", "Venue", NOW, 120, 15)
    result = adjust_charging(
        [out],
        vehicles={(out.identity, out.kind): "Car"},
        distance_km=lambda _: 10,
        estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
        now=NOW,
        navigation=[replace(nav, **change)],
    )
    assert result[0] is out


def test_new_charging_overlap_with_flight_window_retains_original_and_reports_gap():
    out = leg()
    held = []
    result = adjust_charging(
        [out],
        vehicles={(out.identity, out.kind): "Car"},
        distance_km=lambda _: 395,
        estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
        now=NOW,
        options=PlanningOptions(cold_fraction=0, highway_fraction=0),
        protected_windows=[(out.start - timedelta(hours=1), out.start)],
        unavailable=lambda _keys: held.append(True),
    )
    assert result[0] is out and held == [True]


def test_bridge_or_transfer_may_not_leave_before_its_actual_window():
    out = replace(leg(), earliest_start=NOW + timedelta(hours=9))
    assert run([out])[0] is out


def test_missing_return_distance_keeps_outbound_original_as_well():
    out = leg()
    back = leg("meeting_return", origin="Venue", destination="Home", hour=15)
    result = adjust_charging(
        [out, back],
        vehicles={(block.identity, block.kind): "Car" for block in (out, back)},
        distance_km=lambda block: 395 if block is out else None,
        estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
        now=NOW,
    )
    assert result[0] is out and result[1] is back


def test_ongoing_native_route_uses_remaining_eta_and_preserves_actual_start():
    out = replace(leg(hour=0), start=NOW - timedelta(minutes=30), baseline_seconds=12600)
    nav = Navigation("Car", "Venue", NOW, 100, 15)
    requested = []

    def estimate(vehicle, _distance, departure):
        requested.append(departure)
        return EnergySample(vehicle, 75, 0.18, NOW, 50)

    result = adjust_charging(
        [out],
        vehicles={(out.identity, out.kind): "Car"},
        distance_km=lambda _: 395,
        estimate=estimate,
        now=NOW,
        navigation=[nav],
    )[0]
    assert result.start == out.start
    assert result.end == NOW + timedelta(minutes=100)
    assert result.baseline_seconds == out.baseline_seconds
    assert requested == [NOW]


def test_ongoing_leg_without_fresh_bound_navigation_keeps_legacy():
    out = replace(leg(hour=0), start=NOW - timedelta(minutes=30))
    assert run([out])[0] is out


def test_middle_unselected_drive_fences_later_vehicle_energy_without_erasing_prior():
    earlier = leg()
    ambiguous = replace(leg(hour=13), identity="ambiguous", origin="Venue", destination="Other")
    later = replace(leg(hour=17), identity="later", origin="Other", destination="Last")
    result = run(
        [earlier, ambiguous, later],
        vehicles={(earlier.identity, earlier.kind): "Car", (later.identity, later.kind): "Car"},
    )
    assert result[0].start == earlier.start - timedelta(minutes=30)
    assert result[1] is ambiguous
    assert result[2] is later


def test_navigation_notice_never_exports_internal_arrival_charge():
    block = replace(leg(hour=0), start=NOW, end=NOW + timedelta(hours=3))
    nav = Navigation("Car", "Venue", NOW, 127, 23.75)
    result = adjust_charging(
        [block],
        vehicles={(block.identity, block.kind): "Car"},
        distance_km=lambda _: 395,
        estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
        now=NOW,
        navigation=[nav],
    )[0]
    from calendar_apply import build_create_args

    body = build_create_args(result, calendar_id="primary")
    assert result.charging_note == "Car navigation ETA"
    assert "23.75" not in body["description"] and "%" not in body["description"]


def test_exact_fresh_navigation_can_ground_after_an_unselected_drive():
    unknown = replace(leg(hour=0), end=NOW + timedelta(minutes=1))
    current = replace(leg(hour=0), identity="active", start=NOW + timedelta(minutes=2))
    following = replace(leg(hour=4), identity="following")
    nav = Navigation("Car", "Venue", NOW, 25, 40)
    result = adjust_charging(
        [unknown, current, following],
        vehicles={
            (current.identity, current.kind): "Car",
            (following.identity, following.kind): "Car",
        },
        distance_km=lambda _: 200,
        estimate=lambda *_: EnergySample("Car", 75, 0.18, NOW, 90),
        now=NOW,
        navigation=[nav],
        options=PlanningOptions(cold_fraction=0, highway_fraction=0),
    )
    assert result[0] is unknown
    assert result[1].charging_note == "Car navigation ETA"
    assert result[2].charging_note == "1 charging stop, ~30 min"
