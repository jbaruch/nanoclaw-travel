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
- arrival: origin = arrival airport (fixed); dest = `position_at(depart_after)`,
  which is home once a flight has landed at a home airport (#335). The drive
  starts at depart_after; no GPS overlay.

Positions come from one `itinerary.Itinerary` per sweep, built from the TripIt
schedule and the merged byAir ∪ TripIt flights (`itinerary_for`), so a flight
only byAir tracks still moves the operator.
- transfer: both endpoints fixed airports; leave_by = window_end − routed drive.

A leg whose non-fixed endpoint cannot be resolved to an address (`position_at`
returns None off a configured home) is skipped with a diagnostic rather than
routed blind. A trivial routed drive with a boarding block present is suppressed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# The itinerary ships in the co-located travel-core bundle; runtime mount,
# dev-clone sibling fallback for CI (the same pattern as `leg_anchor`).
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = Path(__file__).resolve().parent.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from block_codec import canonical_flight_key, leg_identity  # noqa: E402
from chain import LegKind, plan_chain_legs  # noqa: E402
from chain_builder import build_pair_contexts, group_into_chains  # noqa: E402
from flight_identity import Flight, MergedFlight, merge_flights  # noqa: E402
from itinerary import CANCELLED, DIVERTED, TRIP_LOCATION, Itinerary, Transport  # noqa: E402
from leg_anchor import (  # noqa: E402
    AirportFacts,
    BufferOverrides,
    ConcreteLeg,
    flight_protected_bounds,
    resolve_leg_anchor,
)
from position import position_at, resolve_leg_origin  # noqa: E402
from postarrival import FlightGuard, PostarrivalMeeting, compose_postarrival  # noqa: E402
from reconcile import (  # noqa: E402
    DesiredBlock,
    HeldLeg,
    ParsedBlock,
    ReconcilePlan,
    legacy_keys_for_airport_leg,
    plan_reconcile,
)
from suppression import is_trivial_leg  # noqa: E402

# No real drive to or from an airport runs this long; a longer one means the
# origin or destination resolved to the wrong city (a 16.8 h Schiphol→FCO
# "drive", #323). Such a leg writes no block and reports why.
MAX_AIRPORT_DRIVE = timedelta(hours=6)

RouteFn = Callable[[str, str], "timedelta | None"]
BoardingPresentFn = Callable[[MergedFlight], bool]
LeftTerminalFn = Callable[[MergedFlight, MergedFlight], bool]


@dataclass(frozen=True)
class AirportInfo:
    """Resolved byAir facts for one airport, keyed by IATA in the `airport_info` map."""

    flag: str | None = None
    delay_index: str | None = None
    timezone: str | None = None  # IANA tz — the airport block's local display tz
    lat: float | None = None  # byAir coordinates — the airport's routing point
    lon: float | None = None


@dataclass(frozen=True)
class EngineResult:
    plan: ReconcilePlan
    skipped: tuple[str, ...] = ()  # human diagnostics for legs that couldn't be built
    # Legs that exist but could not be computed this sweep; their blocks are kept.
    held: tuple[HeldLeg, ...] = ()
    origin_sources: tuple[dict[str, str], ...] = ()


# A builder's outcome: the block, or None with a diagnostic, and whether the leg
# is held (it exists but could not be computed) rather than deliberately absent.
Built = tuple["DesiredBlock | None", "str | None", bool]


def _facts_for(flight: MergedFlight, airport_info: dict[str, AirportInfo]) -> AirportFacts:
    dep = airport_info.get(flight.dep_airport, AirportInfo())
    arr = airport_info.get(flight.arr_airport, AirportInfo())
    return AirportFacts(
        dep_flag=dep.flag,
        arr_flag=arr.flag,
        delay_index=dep.delay_index,
        dep_timezone=dep.timezone,
        arr_timezone=arr.timezone,
    )


def _airport_place(iata: str) -> str:
    """The display form of an airport endpoint, `"STN airport"`, written on the
    block. Also the routing form when the airport has no coordinates: the maps
    route can't reliably resolve a bare 3-letter code (#165)."""
    return f"{iata} airport"


def _airport_point(iata: str, airport_info: dict[str, AirportInfo]) -> str:
    """Where the maps route goes for an airport: its byAir coordinates as
    `"lat,lon"` when known, else the text `_airport_place`. Text is geocoded, and
    the backup provider geocoded "SFO airport" as the town of Airport, MO (#334);
    `reconcile_sweep.make_route` refuses a backup answer for a text airport."""
    info = airport_info.get(iata)
    if info is not None and info.lat is not None and info.lon is not None:
        return f"{info.lat},{info.lon}"
    return _airport_place(iata)


def _leg_past(kind: LegKind, concrete: ConcreteLeg, now: datetime) -> bool:
    """True when a leg's drive window has already closed. A departed/completed
    flight needs no drive, and routing the operator's current home to a faraway,
    already-past airport (e.g. home in TN → a London departure that flew last week)
    has no driving route and just fails — so filter these before building instead
    of letting each fail routing with noise (#165)."""
    instant = concrete.window_end if kind is LegKind.AIRPORT_TRANSFER else concrete.anchor
    return instant is not None and instant < now


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


def _implausible(side: str, leg: ConcreteLeg, drive: timedelta, origin: str, dest: str) -> str:
    hours = drive.total_seconds() / 3600
    return (
        f"{side} {leg_identity(leg)}: implausible {hours:.1f} h drive {origin} → {dest}, "
        "not written — check the trip's lodging and transport times in TripIt"
    )


def _build_departure(
    leg: ConcreteLeg,
    *,
    itinerary: Itinerary,
    now: datetime,
    live_origin: str | None,
    origin_sources: list[dict[str, str]],
    route: RouteFn,
    boarding_present: BoardingPresentFn,
    airport_info: dict[str, AirportInfo],
) -> Built:
    anchor = leg.anchor
    assert anchor is not None and leg.dest_airport is not None
    dest = _airport_place(leg.dest_airport)
    dest_point = _airport_point(leg.dest_airport, airport_info)
    planned = position_at(itinerary, anchor)
    if planned.address is None:
        return None, f"departure {leg_identity(leg)}: no origin (position_at unresolved)", True
    approx = route(planned.address, dest_point)
    if approx is None:
        return None, f"departure {leg_identity(leg)}: origin route failed", True
    leave_by = anchor - approx
    resolved = resolve_leg_origin(
        planned, now=now, leave_by=leave_by, drive=approx, live_origin=live_origin
    )
    origin_sources.append(
        {
            "identity": leg_identity(leg),
            "kind": "airport_departure",
            "source": "live_gps" if resolved.source == "live_gps" else "planned",
        }
    )
    origin = resolved.address or planned.address
    drive = route(origin, dest_point)
    if drive is None:
        return None, f"departure {leg_identity(leg)}: route failed", True
    if drive > MAX_AIRPORT_DRIVE:
        return None, _implausible("departure", leg, drive, origin, dest), True
    if is_trivial_leg(drive, presence_block_present=boarding_present(leg.flight)):
        return (
            None,
            f"departure {leg_identity(leg)}: trivial ({int(drive.total_seconds())}s), suppressed",
            False,
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
            destination=dest,
            baseline_seconds=int(drive.total_seconds()),
            anchor=anchor,
            timezone=leg.timezone,
            legacy_keys=_legacy_keys(leg),
        ),
        None,
        False,
    )


def _build_arrival(
    leg: ConcreteLeg,
    *,
    itinerary: Itinerary,
    route: RouteFn,
    boarding_present: BoardingPresentFn,
    airport_info: dict[str, AirportInfo],
) -> Built:
    anchor = leg.anchor
    assert anchor is not None and leg.origin_airport is not None
    origin = _airport_place(leg.origin_airport)
    origin_point = _airport_point(leg.origin_airport, airport_info)
    planned = position_at(itinerary, anchor)
    destination = planned.address
    destination_label = _dest_label(planned)
    if destination is None:
        return None, f"arrival {leg_identity(leg)}: no destination (position_at unresolved)", True
    drive = route(origin_point, destination)
    if drive is None:
        return None, f"arrival {leg_identity(leg)}: route failed", True
    if drive > MAX_AIRPORT_DRIVE:
        return None, _implausible("arrival", leg, drive, origin, destination), True
    if is_trivial_leg(drive, presence_block_present=boarding_present(leg.flight)):
        return None, f"arrival {leg_identity(leg)}: trivial, suppressed", False
    return (
        DesiredBlock(
            identity=leg_identity(leg),
            kind="airport_arrival",
            summary=f"Drive: {leg.origin_airport} → {destination_label}",
            start=anchor,
            end=anchor + drive,
            origin=origin,
            destination=destination,
            baseline_seconds=int(drive.total_seconds()),
            anchor=anchor,
            timezone=leg.timezone,
            legacy_keys=_legacy_keys(leg),
        ),
        None,
        False,
    )


def _build_transfer(
    leg: ConcreteLeg, *, route: RouteFn, airport_info: dict[str, AirportInfo]
) -> Built:
    assert leg.origin_airport is not None and leg.dest_airport is not None
    assert leg.window_start is not None and leg.window_end is not None
    origin = _airport_place(leg.origin_airport)
    dest = _airport_place(leg.dest_airport)
    drive = route(
        _airport_point(leg.origin_airport, airport_info),
        _airport_point(leg.dest_airport, airport_info),
    )
    if drive is None:
        return None, f"transfer {leg_identity(leg)}: route failed", True
    if drive > MAX_AIRPORT_DRIVE:
        return None, _implausible("transfer", leg, drive, origin, dest), True
    leave_by = leg.window_end - drive
    return (
        DesiredBlock(
            identity=leg_identity(leg),
            kind="airport_transfer",
            summary=_summary(leg),
            start=leave_by,
            end=leg.window_end,
            origin=origin,
            destination=dest,
            baseline_seconds=int(drive.total_seconds()),
            anchor=leg.window_start,
            window_end=leg.window_end,
            timezone=leg.timezone,
        ),
        None,
        False,
    )


_LEG_KIND_NAME = {
    LegKind.AIRPORT_DEPARTURE: "airport_departure",
    LegKind.AIRPORT_ARRIVAL: "airport_arrival",
    LegKind.AIRPORT_TRANSFER: "airport_transfer",
}


def _held_leg(concrete: ConcreteLeg, reason: str) -> HeldLeg:
    when = concrete.window_end if concrete.kind is LegKind.AIRPORT_TRANSFER else concrete.anchor
    return HeldLeg(
        identity=leg_identity(concrete),
        kind=_LEG_KIND_NAME[concrete.kind],
        summary=_summary(concrete),
        when=when,
        reason=reason,
    )


def itinerary_for(
    flights: list[MergedFlight],
    schedule: list[dict] | None,
    *,
    home_address: str | None,
    home_airports: frozenset[str] = frozenset(),
    unplaced: tuple[Transport, ...] = (),
) -> Itinerary:
    """The sweep's one itinerary: the schedule plus every merged flight, so a
    flight byAir alone tracks still moves the operator (#335). Each carries
    byAir's live times, its booked departure and its status: a cancelled
    flight takes the schedule's copy with it, a diverted one lands at no known
    airport. `unplaced` are byAir flights whose airports could not be read: he
    still flew then, to somewhere the itinerary judges by the trip."""
    transports = list(unplaced) + [
        Transport(
            f.effective_dep,
            f.effective_arr if f.effective_arr is not None else f.effective_dep,
            f.dep_airport,
            None if f.status == DIVERTED else f.arr_airport,
            scheduled=f.scheduled_dep,
            status=f.status if f.status in (CANCELLED, DIVERTED) else None,
        )
        for f in flights
        # A cancellation needs only its route and booked departure to take
        # the schedule's copy with it; a flight that moves him needs a span.
        if f.status == CANCELLED
        or (f.effective_arr is not None and f.effective_arr > f.effective_dep)
    ]
    return Itinerary(
        schedule, home_address=home_address, home_airports=home_airports, transports=transports
    )


def build_reconcile_plan(
    *,
    flights: list[Flight],
    airport_info: dict[str, AirportInfo],
    current_blocks: list[ParsedBlock],
    route: RouteFn,
    schedule: list[dict] | None = None,
    home_address: str | None = None,
    home_airports: frozenset[str] = frozenset(),
    itinerary: Itinerary | None = None,
    now: datetime,
    live_origin: str | None = None,
    boarding_present: BoardingPresentFn | None = None,
    left_terminal: LeftTerminalFn | None = None,
    overrides: BufferOverrides | None = None,
    extra_desired: list[DesiredBlock] | None = None,
    extra_held: list[HeldLeg] | None = None,
    managed_legacy: frozenset[str] | None = None,
    postarrival_meetings: list[PostarrivalMeeting] | None = None,
) -> EngineResult:
    """Run the full engine pipeline and return the reconcile plan + diagnostics.

    `route(origin, dest)` returns the drive duration or None on failure (a failed
    route skips the leg with a diagnostic, never a blind block). `boarding_present`
    reports whether a flight's boarding block exists (for trivial suppression);
    default assumes present. `left_terminal` supplies same-airport "did you leave"
    geofence evidence. `extra_desired` folds in blocks from another source (the
    meeting-leg source) so both are diffed against the calendar in one reconcile,
    and `extra_held` the legs those sources could not compute, so their blocks are
    kept (#334). `postarrival_meetings` are timed in-person meetings composed with
    inbound arrivals before reconcile (#350): a reachable ongoing meeting replaces
    airport-to-lodging, and no desired drive may start inside a flight interval.
    `managed_legacy`, when given, is passed through to
    `plan_reconcile` to scope which legacy generations the engine may converge /
    orphan-delete.

    A leg that could not be computed — no resolvable position, a failed or
    implausible route, a drive window already past — is held, never dropped: its
    existing block stays as it is. Only a deliberately suppressed leg (a trivial
    drive) leaves the desired set, so its block is deleted.

    `route` runs to completion — there is no routing deadline (#211). A hung
    provider is bounded by the maps client's own per-call timeout (a failed leg
    returns None and is skipped with a diagnostic), not by abandoning the plan.
    """
    boarding_present = boarding_present or (lambda _flight: True)
    if overrides is None:
        overrides = BufferOverrides()
    merged = merge_flights(flights)
    # byAir governs status: a cancelled flight has no drive to or from it.
    chains = group_into_chains([f for f in merged if f.status != CANCELLED])

    if itinerary is None:
        itinerary = itinerary_for(
            merged, schedule, home_address=home_address, home_airports=home_airports
        )

    desired: list[DesiredBlock] = []
    skipped: list[str] = []
    held: list[HeldLeg] = []
    origin_sources: list[dict[str, str]] = []
    guards: list[FlightGuard] = []

    for chain in chains:
        closing = chain[-1]
        for merged in chain:
            if merged.status == DIVERTED or merged.effective_arr is None:
                continue
            facts = _facts_for(merged, airport_info)
            guard_start, ready_at = flight_protected_bounds(
                merged, facts=facts, overrides=overrides
            )
            guards.append(
                FlightGuard(
                    identity=canonical_flight_key(merged),
                    guard_start=guard_start,
                    ready_at=ready_at,
                    airport_code=merged.arr_airport,
                    airport_point=_airport_point(merged.arr_airport, airport_info),
                    airport_label=_airport_place(merged.arr_airport),
                    timezone=facts.arr_timezone,
                    chain_closing=merged is closing,
                )
            )
        contexts = build_pair_contexts(chain, schedule=schedule, left_terminal=left_terminal)
        planned_legs = plan_chain_legs(chain, contexts)
        for planned in planned_legs:
            if (
                planned.kind is not LegKind.AIRPORT_DEPARTURE
                and planned.from_flight is not None
                and planned.from_flight.status == DIVERTED
            ):
                # A diverted flight lands somewhere else: no drive leaves its
                # booked destination until byAir names where it landed.
                diverted = planned.from_flight
                skipped.append(
                    f"{planned.kind.value} after diverted flight "
                    f"{diverted.dep_airport}-{diverted.arr_airport}: suppressed"
                )
                continue
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
                if _leg_past(planned.kind, concrete, now):
                    diag = f"transfer {leg_identity(concrete)}: past, skipped"
                    skipped.append(diag)
                    held.append(_held_leg(concrete, diag))
                    continue
                block, diag, hold = _build_transfer(
                    concrete, route=route, airport_info=airport_info
                )
            elif planned.kind is LegKind.AIRPORT_DEPARTURE:
                flight = planned.to_flight
                if flight is None:
                    skipped.append("departure leg missing a flight")
                    continue
                concrete = resolve_leg_anchor(
                    planned, facts=_facts_for(flight, airport_info), overrides=overrides
                )
                if _leg_past(planned.kind, concrete, now):
                    diag = f"departure {leg_identity(concrete)}: past, skipped"
                    skipped.append(diag)
                    held.append(_held_leg(concrete, diag))
                    continue
                block, diag, hold = _build_departure(
                    concrete,
                    itinerary=itinerary,
                    now=now,
                    live_origin=live_origin,
                    origin_sources=origin_sources,
                    route=route,
                    boarding_present=boarding_present,
                    airport_info=airport_info,
                )
            else:  # AIRPORT_ARRIVAL
                flight = planned.from_flight
                if flight is None:
                    skipped.append("arrival leg missing a flight")
                    continue
                concrete = resolve_leg_anchor(
                    planned, facts=_facts_for(flight, airport_info), overrides=overrides
                )
                if _leg_past(planned.kind, concrete, now):
                    diag = f"arrival {leg_identity(concrete)}: past, skipped"
                    skipped.append(diag)
                    held.append(_held_leg(concrete, diag))
                    continue
                block, diag, hold = _build_arrival(
                    concrete,
                    itinerary=itinerary,
                    route=route,
                    boarding_present=boarding_present,
                    airport_info=airport_info,
                )
            if block is not None:
                desired.append(block)
            if diag is not None:
                skipped.append(diag)
                if hold:
                    held.append(_held_leg(concrete, diag))

    if extra_desired:
        desired.extend(extra_desired)
    held.extend(extra_held or [])

    if guards:

        def lodging_lookup(at: datetime) -> str | None:
            pos = itinerary.addressable_lodging_in_period(at)
            return pos.address if pos is not None else None

        def fallback_lookup(at: datetime) -> str | None:
            # This fallback is a MEETING return, not the ordinary airport leg.
            # A trip city cannot supply a drivable meeting endpoint (#356).
            position = itinerary.position_at(at)
            return None if position.source == TRIP_LOCATION else position.address

        desired, held, post_diags = compose_postarrival(
            guards=guards,
            meetings=tuple(postarrival_meetings or ()),
            desired=desired,
            held=held,
            lodging_lookup=lodging_lookup,
            fallback_lookup=fallback_lookup,
            route=route,
            current_blocks=current_blocks,
        )
        skipped.extend(post_diags)

    held_keys = frozenset(leg.key for leg in held)
    if managed_legacy is None:
        plan = plan_reconcile(desired, current_blocks, held=held_keys, now=now)
    else:
        plan = plan_reconcile(
            desired, current_blocks, managed_legacy=managed_legacy, held=held_keys, now=now
        )
    return EngineResult(
        plan=plan, skipped=tuple(skipped), held=tuple(held), origin_sources=tuple(origin_sources)
    )
