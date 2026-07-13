"""Leg anchor derivation — the §B buffer math — pure, no I/O, no clock.

Turns a `PlannedLeg` (from `chain.py`) plus the resolved airport facts into a
`ConcreteLeg` carrying the anchor instant(s) the reconcile will place the drive
block against. This is the §B table of issue #156:

    leg type          anchor
    airport_departure arrive_by   = effective_dep − clearance
    airport_arrival   depart_after = effective_arr + post_arrival
    airport_transfer  window       = [effective_arr(N) + post_arrival(N) …
                                       effective_dep(N+1) − clearance(N+1)]

The airport asymmetry the owner called out — you don't arrive at the flight moment
and don't leave the instant the plane lands — is captured entirely here by the
anchor + buffer, not by a separate engine. Buffer POLICY (domestic / international,
Schengen-as-domestic, delay-index nudge, config overrides) stays centralized in
`airport_lead`; this module only calls it and does the arithmetic.

Pure: the caller resolves each airport's byAir facts (country flag, delay index)
and any config buffer overrides upstream and passes them in. `effective_dep` /
`effective_arr` are the MergedFlight's best-known instants (live byAir time when
present, else scheduled — byAir wins, #156 Decision 4).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from chain import LegKind, PlannedLeg
from flight_identity import MergedFlight

# airport_lead ships in the co-located travel-core bundle; runtime mount, dev-clone
# sibling fallback for CI.
_BUNDLE_DIR = Path(__file__).resolve().parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from airport_lead import (  # noqa: E402
    resolve_departure_clearance_minutes,
    resolve_post_arrival_minutes,
)


@dataclass(frozen=True)
class AirportFacts:
    """The byAir-derived facts a leg needs about one flight's airports.

    Resolved upstream from `get_airport` payloads. `dep_flag` / `arr_flag` are the
    `countryFlag` emoji (decoded to domestic/international by `airport_lead`);
    `delay_index` is byAir's `delay.index` (`low` / `medium` / `high`) driving the
    clearance nudge.
    """

    dep_flag: str | None = None
    arr_flag: str | None = None
    delay_index: str | None = None
    dep_timezone: str | None = None  # IANA tz of the departure airport
    arr_timezone: str | None = None  # IANA tz of the arrival airport


@dataclass(frozen=True)
class BufferOverrides:
    """Optional per-run config overrides for the airport_lead base policy. A None
    field means "use the airport_lead default." Mirrors the config.json keys."""

    clearance_domestic: int | None = None
    clearance_international: int | None = None
    post_arrival_domestic: int | None = None
    post_arrival_intl_us: int | None = None
    post_arrival_intl_abroad: int | None = None


_NO_OVERRIDES = BufferOverrides()


@dataclass(frozen=True)
class ConcreteLeg:
    """A leg with its anchor(s) resolved. Fixed airport endpoints are IATA codes;
    the non-fixed endpoint is filled later by position_at. Departure and arrival
    carry a single `anchor`; transfer carries `window_start` / `window_end`."""

    kind: LegKind
    flight: MergedFlight
    origin_airport: str | None = None
    dest_airport: str | None = None
    anchor: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    partner_flight: MergedFlight | None = None
    timezone: str | None = None  # IANA tz to create the block in (the airport's)


def _clearance(facts: AirportFacts, ov: BufferOverrides) -> timedelta:
    kwargs: dict[str, int] = {}
    if ov.clearance_domestic is not None:
        kwargs["domestic_minutes"] = ov.clearance_domestic
    if ov.clearance_international is not None:
        kwargs["international_minutes"] = ov.clearance_international
    minutes = resolve_departure_clearance_minutes(
        dep_flag=facts.dep_flag,
        arr_flag=facts.arr_flag,
        delay_index=facts.delay_index,
        **kwargs,
    )
    return timedelta(minutes=minutes)


def _post_arrival(facts: AirportFacts, ov: BufferOverrides) -> timedelta:
    kwargs: dict[str, int] = {}
    if ov.post_arrival_domestic is not None:
        kwargs["domestic_minutes"] = ov.post_arrival_domestic
    if ov.post_arrival_intl_us is not None:
        kwargs["intl_to_us_minutes"] = ov.post_arrival_intl_us
    if ov.post_arrival_intl_abroad is not None:
        kwargs["intl_abroad_minutes"] = ov.post_arrival_intl_abroad
    minutes = resolve_post_arrival_minutes(
        dep_flag=facts.dep_flag, arr_flag=facts.arr_flag, **kwargs
    )
    return timedelta(minutes=minutes)


def _require_arr(flight: MergedFlight) -> datetime:
    arr = flight.effective_arr
    if arr is None:
        raise ValueError(
            f"leg anchor needs an arrival time for flight {sorted(flight.byair_flight_ids)} "
            f"{flight.dep_airport}->{flight.arr_airport}; none resolved"
        )
    return arr


def resolve_leg_anchor(
    leg: PlannedLeg,
    *,
    facts: AirportFacts,
    partner_facts: AirportFacts | None = None,
    overrides: BufferOverrides = _NO_OVERRIDES,
) -> ConcreteLeg:
    """Compute a PlannedLeg's concrete anchor(s) from flight times + buffers (§B).

    `facts` describes the leg's primary flight; `partner_facts` the transfer's
    second flight (required for transfers). Departure anchors on the departure
    deadline, arrival on the earliest the drive home can start, transfer on the
    window between landing-plus-buffer and next-departure-minus-clearance.
    """
    if leg.kind is LegKind.AIRPORT_DEPARTURE:
        flight = leg.to_flight
        if flight is None:
            raise ValueError("airport_departure leg missing to_flight")
        return ConcreteLeg(
            kind=leg.kind,
            flight=flight,
            dest_airport=flight.dep_airport,
            anchor=flight.effective_dep - _clearance(facts, overrides),
            timezone=facts.dep_timezone,
        )

    if leg.kind is LegKind.AIRPORT_ARRIVAL:
        flight = leg.from_flight
        if flight is None:
            raise ValueError("airport_arrival leg missing from_flight")
        return ConcreteLeg(
            kind=leg.kind,
            flight=flight,
            origin_airport=flight.arr_airport,
            anchor=_require_arr(flight) + _post_arrival(facts, overrides),
            timezone=facts.arr_timezone,
        )

    if leg.kind is LegKind.AIRPORT_TRANSFER:
        earlier, later = leg.from_flight, leg.to_flight
        if earlier is None or later is None:
            raise ValueError("airport_transfer leg missing from_flight or to_flight")
        if partner_facts is None:
            raise ValueError("airport_transfer leg needs partner_facts for the later flight")
        return ConcreteLeg(
            kind=leg.kind,
            flight=earlier,
            partner_flight=later,
            origin_airport=earlier.arr_airport,
            dest_airport=later.dep_airport,
            window_start=_require_arr(earlier) + _post_arrival(facts, overrides),
            window_end=later.effective_dep - _clearance(partner_facts, overrides),
            timezone=facts.arr_timezone,
        )

    raise ValueError(f"resolve_leg_anchor does not handle leg kind {leg.kind}")
