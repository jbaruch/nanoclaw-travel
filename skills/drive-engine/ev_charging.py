"""Pure, conservative EV time planning over the complete chronological drive set.

Inputs are measurements from the supported Tesla estimate provider, route distance,
and explicitly grounded charging observations. No I/O, vehicle control, charger
selection or persistence lives here. A stop is a minimum energy stop assuming a
full usable recharge; configured minutes are an allowance, never a station promise.
The measured weighted driving rate is NOT a measured highway-only rate. Explicit
cold/highway allowances remain separate from that measurement.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from reconcile import DesiredBlock


@dataclass(frozen=True)
class EnergyGap:
    """An existing unplanned drive; unknown start/vehicle fences all possible uses."""

    start: datetime | None
    vehicle: str | None = None


MAX_STOPS = 24
MAX_LEGS = 2000
FORECAST_HORIZON = timedelta(days=14)
MEASUREMENT_AGE = timedelta(days=90)
NAVIGATION_AGE = timedelta(minutes=5)
NAVIGATION_HORIZON = timedelta(minutes=15)


def finite(value: object, low: float, high: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and low <= value <= high
    )


def place(value: str) -> str:
    return " ".join(value.split()).casefold()


@dataclass(frozen=True)
class PlanningOptions:
    stop_minutes: int = 30
    reserve_percent: int = 10
    highway_fraction: float = 0.1
    cold_fraction: float = 0.2

    def __post_init__(self):
        if (
            type(self.stop_minutes) is not int
            or not 1 <= self.stop_minutes <= 240
            or type(self.reserve_percent) is not int
            or not 0 <= self.reserve_percent <= 50
            or not finite(self.highway_fraction, 0, 2)
            or not finite(self.cold_fraction, 0, 2)
        ):
            raise ValueError("Use bounded charging-time, reserve and energy allowances.")


@dataclass(frozen=True)
class EnergySample:
    vehicle: str
    capacity_kwh: float
    measured_kwh_per_km: float
    measured_at: datetime
    departure_percent: float

    def valid(self, vehicle: str, now: datetime) -> bool:
        return (
            self.vehicle == vehicle
            and finite(self.capacity_kwh, 0.001, 1000)
            and finite(self.measured_kwh_per_km, 0.000001, 10)
            and finite(self.departure_percent, 0, 100)
            and self.measured_at.tzinfo is not None
            and now - MEASUREMENT_AGE <= self.measured_at <= now
        )


@dataclass(frozen=True)
class DestinationCharge:
    """Positive owner/provider evidence, bound to one vehicle, place and time.

    Mere lodging, multi-day duration, a charger POI or a guessed overnight target
    is not evidence. This record describes a known delivered/forecast energy
    amount and its availability instant; the adapter must retain its provenance.
    """

    vehicle: str
    destination: str
    available_at: datetime
    energy_kwh: float


@dataclass(frozen=True)
class Navigation:
    """Cached owner-only active route, never a request to wake or navigate a car."""

    vehicle: str
    destination: str
    observed_at: datetime
    minutes_to_arrival: float
    arrival_percent: float

    def matches(self, block: DesiredBlock, vehicle: str, now: datetime) -> bool:
        return (
            self.vehicle == vehicle
            and place(self.destination) == place(block.destination)
            and bool(place(self.destination))
            and self.observed_at.tzinfo is not None
            and now - NAVIGATION_AGE <= self.observed_at <= now
            and block.end > now
            and block.start <= now + NAVIGATION_HORIZON
            and finite(self.minutes_to_arrival, 0.01, 1440)
            and finite(self.arrival_percent, 0, 100)
        )


Estimate = Callable[[str, float, datetime], EnergySample | None]


def _shift(block: DesiredBlock, extra: timedelta, note: str) -> DesiredBlock:
    # Return/arrival drives start after a preceding event; all other drives
    # arrive by their anchor. This preserves their existing scheduling contract.
    after = block.kind in {"meeting_return", "airport_arrival", "lodging_return"}
    return replace(
        block,
        start=block.start if after else block.start - extra,
        end=block.end + extra if after else block.end,
        charging_note=note,
    )


def adjust_charging(
    blocks: Sequence[DesiredBlock],
    *,
    vehicles: Mapping[tuple[str, str], str],
    distance_km: Callable[[DesiredBlock], float | None],
    estimate: Estimate,
    now: datetime,
    options: PlanningOptions | None = None,
    destination_charges: Sequence[DestinationCharge] = (),
    navigation: Sequence[Navigation] = (),
    protected_windows: Sequence[tuple[datetime, datetime]] = (),
    energy_gaps: Sequence[EnergyGap] = (),
    unavailable: Callable[[frozenset[tuple[str, str]]], None] | None = None,
) -> list[DesiredBlock]:
    """Return adjusted blocks; ungrounded dependent legs retain ORIGINAL objects.

    Every later leg consumes the prior leg's remaining energy. Independently
    forecasted return percentages never replenish it. Only exact positive
    destination-charge evidence can add energy between connected legs. Missing
    distance, measurement, vehicle or route continuity keeps the whole vehicle
    dependent chain unchanged instead of producing an optimistic return battery.
    Out-of-horizon legs remain unchanged without suppressing nearer drives.
    Disconnected endpoints do not replenish the same vehicle's remaining energy.

    A bounded fixed-point pass forecasts the first leg at its actual earlier
    leave-by, rather than counting charging that would occur after it must leave.
    No effect retry is involved: estimate is a read-only dependency.
    """
    original = list(blocks)
    options = options or PlanningOptions()
    if now.tzinfo is None or len(original) > MAX_LEGS:
        return original
    grouped: dict[str, list[tuple[int, DesiredBlock]]] = {}
    eligible_keys = {
        (vehicles.get((block.identity, block.kind)), block.identity, block.kind)
        for block in original
        if isinstance(vehicles.get((block.identity, block.kind)), str)
        and vehicles.get((block.identity, block.kind))
        and block.end > now
        and block.start <= now + FORECAST_HORIZON
    }
    return_predecessors = {
        "meeting_return": "meeting_outbound",
        "lodging_return": "lodging_outbound",
        "airport_arrival": "airport_departure",
    }
    unknown_starts = sorted(
        block.start
        for block in original
        if (block.identity, block.kind) not in vehicles
        and block.end > now
        and block.start <= now + FORECAST_HORIZON
    )
    for index, block in enumerate(original):
        vehicle = vehicles.get((block.identity, block.kind))
        if (
            isinstance(vehicle, str)
            and vehicle.strip()
            and block.end > now
            and block.start <= now + FORECAST_HORIZON
        ):
            grouped.setdefault(vehicle, []).append((index, block))
    for vehicle, chain in grouped.items():
        starts = sorted(
            [
                *unknown_starts,
                *(
                    gap.start or now
                    for gap in energy_gaps
                    if gap.vehicle is None or gap.vehicle == vehicle
                ),
            ]
        )
        chain.sort(key=lambda item: (item[1].start, item[1].end, item[0]))
        segments: list[list[tuple[int, DesiredBlock]]] = []
        segment: list[tuple[int, DesiredBlock]] = []
        unknown_index = 0
        grounded = True
        last_unknown: datetime | None = None
        ungrounded = set()
        for item in chain:
            _, block = item
            crossed_unknown = False
            overlapped_unknown = False
            while unknown_index < len(starts) and starts[unknown_index] < block.end:
                grounded = False
                crossed_unknown = True
                overlapped_unknown |= starts[unknown_index] > block.start
                last_unknown = starts[unknown_index]
                unknown_index += 1
            predecessor = return_predecessors.get(block.kind)
            if predecessor and (vehicle, block.identity, predecessor) not in eligible_keys:
                grounded = False
                crossed_unknown = True
            if crossed_unknown and segment:
                segments.append(segment)
                segment = []
            if not overlapped_unknown and any(
                route.matches(block, vehicle, now)
                and (last_unknown is None or route.observed_at >= last_unknown)
                and route.arrival_percent >= options.reserve_percent
                for route in navigation
            ):
                # Exact fresh active-route arrival energy independently grounds
                # this leg and later uses, even after an unknown intervening drive.
                grounded = True
            if grounded:
                segment.append(item)
            else:
                ungrounded.add((block.identity, block.kind))
        if segment:
            segments.append(segment)
        if ungrounded and unavailable is not None:
            unavailable(frozenset(ungrounded))
        for segment in segments:
            planned = _vehicle_chain(
                segment,
                vehicle,
                distance_km,
                estimate,
                now,
                options,
                destination_charges,
                navigation,
                protected_windows,
            )
            if planned is not None:
                # An earlier departure produced by charging/navigation also
                # cannot cross a possible drive that the original span avoided.
                crossed = next(
                    (
                        index
                        for index, block in enumerate(planned)
                        if any(block.start < start < block.end for start in starts)
                    ),
                    len(planned),
                )
                if crossed < len(planned) and unavailable is not None:
                    unavailable(
                        frozenset((block.identity, block.kind) for _, block in segment[crossed:])
                    )
                for (index, _), block in zip(segment[:crossed], planned[:crossed], strict=True):
                    original[index] = block
            elif unavailable is not None:
                unavailable(frozenset((block.identity, block.kind) for _, block in segment))
    return original


def _vehicle_chain(
    chain, vehicle, distance_km, estimate, now, options, charges, navigation, windows
):
    distances: list[float] = []
    prior: DesiredBlock | None = None
    for _, block in chain:
        distance = distance_km(block)
        if (
            not finite(distance, 0.001, 100000)
            or block.start.tzinfo is None
            or block.end.tzinfo is None
            or block.end <= block.start
            or block.start > now + FORECAST_HORIZON
            or (prior is not None and prior.end > block.start)
        ):
            return None
        distances.append(float(distance))
        prior = block
    first = chain[0][1]
    departure = max(now, first.start)
    for _ in range(4):
        sample = estimate(vehicle, distances[0], departure)
        if sample is None or not sample.valid(vehicle, now):
            return None
        capacity = sample.capacity_kwh
        remaining = capacity * sample.departure_percent / 100
        reserve = capacity * options.reserve_percent / 100
        per_stop = capacity - reserve
        results: list[DesiredBlock] = []
        used_charges: set[int] = set()
        for position, ((_, block), distance) in enumerate(zip(chain, distances, strict=True)):
            charging_ready: list[datetime] = []
            if position:
                previous = results[-1]
                for index, charge in enumerate(charges):
                    if (
                        index not in used_charges
                        and charge.vehicle == vehicle
                        and place(previous.destination) == place(block.origin)
                        and place(charge.destination) == place(block.origin)
                        and charge.available_at.tzinfo is not None
                        and previous.end <= charge.available_at <= block.start
                        and finite(charge.energy_kwh, 0, capacity)
                    ):
                        remaining = min(capacity, remaining + charge.energy_kwh)
                        used_charges.add(index)
                        charging_ready.append(charge.available_at)
            energy = (
                distance
                * sample.measured_kwh_per_km
                * (1 + options.cold_fraction)
                * (1 + options.highway_fraction)
            )
            stops = max(0, math.ceil(max(0, energy + reserve - remaining) / per_stop - 1e-12))
            if stops > MAX_STOPS:
                return None
            remaining += stops * per_stop - energy
            extra = timedelta(minutes=stops * options.stop_minutes)
            result = block
            if stops:
                noun = "stop" if stops == 1 else "stops"
                note = f"{stops} charging {noun}, ~{stops * options.stop_minutes} min"
                result = _shift(block, extra, note)
            routes = [route for route in navigation if route.matches(block, vehicle, now)]
            native_route_used = False
            if len(routes) > 1:
                return None
            if routes and routes[0].arrival_percent >= options.reserve_percent:
                native_route_used = True
                nav = routes[0]
                duration = timedelta(minutes=nav.minutes_to_arrival)
                result = _shift(
                    block,
                    duration - (block.end - block.start),
                    "Car navigation ETA",
                )
                if block.start < now:
                    # Native remaining ETA is measured from now. Preserve the
                    # actual block's past start rather than backdating a new
                    # departure or treating remaining minutes as total time.
                    result = replace(
                        result,
                        start=block.start,
                        end=now + duration,
                    )
                remaining = capacity * nav.arrival_percent / 100
            if (
                (result.start < now and not (native_route_used and result.start == block.start))
                or (block.earliest_start is not None and result.start < block.earliest_start)
                or any(result.start < ready for ready in charging_ready)
                or (results and results[-1].end > result.start)
            ):
                return None
            if result != block and any(
                (result.start < block.start and result.start < end and block.start > start)
                or (result.end > block.end and block.end < end and result.end > start)
                for start, end in windows
            ):
                return None
            results.append(result)
        actual_departure = max(now, results[0].start)
        if actual_departure == departure:
            return results
        departure = actual_departure
    return None
