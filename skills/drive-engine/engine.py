"""Engine orchestration — assemble the pure pipeline into a reconcile plan.

Ties the pure modules together: normalized `Flight`s → merged union → per-trip
chains → connection classification → planned legs → §B anchors → routed drives with
GPS-overlaid origins → desired blocks → the reconcile diff against the calendar.

Dependency-injected so the whole flow is testable headless: the caller resolves
everything that needs I/O — airport facts (byAir `get_airport`), routed drive
seconds (`maps_client`), the current calendar blocks, the live-location fix, and
the boarding-block / left-terminal predicates — and passes them in. `main()` wires
the real clients; this function stays pure over its inputs.

Per-leg endpoint resolution (the routing ↔ leave-by ↔ origin interplay of §A/§B):

- departure: dest = departure airport (fixed); origin = `position_at(anchor)`, then
  routed, then GPS-overlaid when imminent, then re-routed from the resolved origin.
  leave_by = arrive_by − routed drive.
- arrival: origin = arrival airport (fixed); dest = `position_at(depart_after)`. The
  drive starts at depart_after; no GPS overlay (the operator is at the airport).
- transfer: both endpoints fixed airports; leave_by = window_end − routed drive.

A leg whose non-fixed endpoint cannot be resolved to an address (`position_at`
returns None off a configured home) is skipped with a diagnostic rather than
routed blind. A trivial routed drive with a boarding block present is suppressed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from block_codec import leg_identity
from chain import LegKind, plan_chain_legs
from chain_builder import build_pair_contexts, group_into_chains
from flight_identity import Flight, MergedFlight, merge_flights
from leg_anchor import AirportFacts, BufferOverrides, ConcreteLeg, resolve_leg_anchor
from position import position_at, resolve_leg_origin
from reconcile import (
    DesiredBlock,
    ParsedBlock,
    ReconcilePlan,
    legacy_keys_for_airport_leg,
    plan_reconcile,
)
from suppression import is_trivial_leg

RouteFn = Callable[[str, str], "timedelta | None"]
BoardingPresentFn = Callable[[MergedFlight], bool]
LeftTerminalFn = Callable[[MergedFlight, MergedFlight], bool]


@dataclass(frozen=True)
class AirportInfo:
    """Resolved byAir facts for one airport, keyed by IATA in the `airport_info` map."""

    flag: str | None = None
    delay_index: str | None = None


@dataclass(frozen=True)
class EngineResult:
    plan: ReconcilePlan
    skipped: tuple[str, ...] = ()  # human diagnostics for legs that couldn't be built


def _facts_for(flight: MergedFlight, airport_info: dict[str, AirportInfo]) -> AirportFacts:
    dep = airport_info.get(flight.dep_airport, AirportInfo())
    arr = airport_info.get(flight.arr_airport, AirportInfo())
    return AirportFacts(dep_flag=dep.flag, arr_flag=arr.flag, delay_index=dep.delay_index)


def _dest_label(planned) -> str:
    """A human label for a resolved drive-home destination: 'home' only when it
    really is the static home, else the lodging/anchor address (never a lie)."""
    if planned.source == "home":
        return "home"
    return planned.address or "destination"


def _summary(leg: ConcreteLeg) -> str:
    if leg.kind is LegKind.AIRPORT_DEPARTURE:
        return f"Drive: → {leg.dest_airport}"
    if leg.kind is LegKind.AIRPORT_ARRIVAL:
        # Arrival's real destination is resolved later; _build_arrival overrides
        # this with the actual place. Fallback label only.
        return f"Drive: {leg.origin_airport} → destination"
    return f"Drive: {leg.origin_airport} → {leg.dest_airport}"


def _legacy_keys(leg: ConcreteLeg) -> frozenset[tuple[str, str, str]]:
    kind = (
        "airport_departure"
        if leg.kind is LegKind.AIRPORT_DEPARTURE
        else "airport_arrival"
        if leg.kind is LegKind.AIRPORT_ARRIVAL
        else "airport_transfer"
    )
    return legacy_keys_for_airport_leg(kind, leg.flight.byair_flight_ids)


def _build_departure(
    leg: ConcreteLeg,
    *,
    schedule: list[dict] | None,
    home_address: str | None,
    now: datetime,
    live_origin: str | None,
    route: RouteFn,
    boarding_present: BoardingPresentFn,
) -> tuple[DesiredBlock | None, str | None]:
    anchor = leg.anchor
    assert anchor is not None and leg.dest_airport is not None
    planned = position_at(schedule, anchor, home_address=home_address)
    if planned.address is None:
        return None, f"departure {leg_identity(leg)}: no origin (position_at unresolved)"
    approx = route(planned.address, leg.dest_airport)
    if approx is None:
        return None, f"departure {leg_identity(leg)}: origin route failed"
    leave_by = anchor - approx
    resolved = resolve_leg_origin(
        planned, now=now, leave_by=leave_by, drive=approx, live_origin=live_origin
    )
    origin = resolved.address or planned.address
    drive = route(origin, leg.dest_airport)
    if drive is None:
        return None, f"departure {leg_identity(leg)}: route failed"
    if is_trivial_leg(drive, presence_block_present=boarding_present(leg.flight)):
        return (
            None,
            f"departure {leg_identity(leg)}: trivial ({int(drive.total_seconds())}s), suppressed",
        )
    leave_by = anchor - drive
    return (
        DesiredBlock(
            identity=leg_identity(leg),
            kind="airport_departure",
            summary=_summary(leg),
            start=leave_by,
            end=anchor,
            origin=origin,
            destination=leg.dest_airport,
            baseline_seconds=int(drive.total_seconds()),
            anchor=anchor,
            legacy_keys=_legacy_keys(leg),
        ),
        None,
    )


def _build_arrival(
    leg: ConcreteLeg,
    *,
    schedule: list[dict] | None,
    home_address: str | None,
    route: RouteFn,
    boarding_present: BoardingPresentFn,
) -> tuple[DesiredBlock | None, str | None]:
    anchor = leg.anchor
    assert anchor is not None and leg.origin_airport is not None
    planned = position_at(schedule, anchor, home_address=home_address)
    if planned.address is None:
        return None, f"arrival {leg_identity(leg)}: no destination (position_at unresolved)"
    drive = route(leg.origin_airport, planned.address)
    if drive is None:
        return None, f"arrival {leg_identity(leg)}: route failed"
    if is_trivial_leg(drive, presence_block_present=boarding_present(leg.flight)):
        return None, f"arrival {leg_identity(leg)}: trivial, suppressed"
    return (
        DesiredBlock(
            identity=leg_identity(leg),
            kind="airport_arrival",
            summary=f"Drive: {leg.origin_airport} → {_dest_label(planned)}",
            start=anchor,
            end=anchor + drive,
            origin=leg.origin_airport,
            destination=planned.address,
            baseline_seconds=int(drive.total_seconds()),
            anchor=anchor,
            legacy_keys=_legacy_keys(leg),
        ),
        None,
    )


def _build_transfer(leg: ConcreteLeg, *, route: RouteFn) -> tuple[DesiredBlock | None, str | None]:
    assert leg.origin_airport is not None and leg.dest_airport is not None
    assert leg.window_start is not None and leg.window_end is not None
    drive = route(leg.origin_airport, leg.dest_airport)
    if drive is None:
        return None, f"transfer {leg_identity(leg)}: route failed"
    leave_by = leg.window_end - drive
    return (
        DesiredBlock(
            identity=leg_identity(leg),
            kind="airport_transfer",
            summary=_summary(leg),
            start=leave_by,
            end=leg.window_end,
            origin=leg.origin_airport,
            destination=leg.dest_airport,
            baseline_seconds=int(drive.total_seconds()),
            anchor=leg.window_start,
            window_end=leg.window_end,
        ),
        None,
    )


def build_reconcile_plan(
    *,
    flights: list[Flight],
    airport_info: dict[str, AirportInfo],
    current_blocks: list[ParsedBlock],
    route: RouteFn,
    schedule: list[dict] | None = None,
    home_address: str | None = None,
    now: datetime,
    live_origin: str | None = None,
    boarding_present: BoardingPresentFn | None = None,
    left_terminal: LeftTerminalFn | None = None,
    overrides: BufferOverrides | None = None,
    extra_desired: list[DesiredBlock] | None = None,
    managed_legacy: frozenset[str] | None = None,
) -> EngineResult:
    """Run the full engine pipeline and return the reconcile plan + diagnostics.

    `route(origin, dest)` returns the drive duration or None on failure (a failed
    route skips the leg with a diagnostic, never a blind block). `boarding_present`
    reports whether a flight's boarding block exists (for trivial suppression);
    default assumes present. `left_terminal` supplies same-airport "did you leave"
    geofence evidence. `extra_desired` folds in blocks from another source (the
    meeting-leg source) so both are diffed against the calendar in one reconcile.
    `managed_legacy`, when given, is passed through to `plan_reconcile` to scope
    which legacy generations the engine may converge / orphan-delete.
    """
    boarding_present = boarding_present or (lambda _flight: True)
    if overrides is None:
        overrides = BufferOverrides()
    merged = merge_flights(flights)
    chains = group_into_chains(merged)

    desired: list[DesiredBlock] = []
    skipped: list[str] = []

    for chain in chains:
        contexts = build_pair_contexts(chain, schedule=schedule, left_terminal=left_terminal)
        for planned in plan_chain_legs(chain, contexts):
            # plan_chain_legs guarantees the right endpoint per kind, but narrow it
            # explicitly here so _facts_for receives a non-None flight (no ignore).
            if planned.kind is LegKind.AIRPORT_TRANSFER:
                earlier, later = planned.from_flight, planned.to_flight
                if earlier is None or later is None:
                    skipped.append("transfer leg missing a flight endpoint")
                    continue
                concrete = resolve_leg_anchor(
                    planned,
                    facts=_facts_for(earlier, airport_info),
                    partner_facts=_facts_for(later, airport_info),
                    overrides=overrides,
                )
                block, diag = _build_transfer(concrete, route=route)
            elif planned.kind is LegKind.AIRPORT_DEPARTURE:
                flight = planned.to_flight
                if flight is None:
                    skipped.append("departure leg missing a flight")
                    continue
                concrete = resolve_leg_anchor(
                    planned, facts=_facts_for(flight, airport_info), overrides=overrides
                )
                block, diag = _build_departure(
                    concrete,
                    schedule=schedule,
                    home_address=home_address,
                    now=now,
                    live_origin=live_origin,
                    route=route,
                    boarding_present=boarding_present,
                )
            else:  # AIRPORT_ARRIVAL
                flight = planned.from_flight
                if flight is None:
                    skipped.append("arrival leg missing a flight")
                    continue
                concrete = resolve_leg_anchor(
                    planned, facts=_facts_for(flight, airport_info), overrides=overrides
                )
                block, diag = _build_arrival(
                    concrete,
                    schedule=schedule,
                    home_address=home_address,
                    route=route,
                    boarding_present=boarding_present,
                )
            if block is not None:
                desired.append(block)
            if diag is not None:
                skipped.append(diag)

    if extra_desired:
        desired.extend(extra_desired)

    reconcile_kwargs = {} if managed_legacy is None else {"managed_legacy": managed_legacy}
    plan = plan_reconcile(desired, current_blocks, **reconcile_kwargs)
    return EngineResult(plan=plan, skipped=tuple(skipped))
