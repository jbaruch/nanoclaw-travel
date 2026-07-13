"""Flight-chain connection classification — pure, no I/O, no clock.

A drive engine must decide, for each consecutive pair of flights in a trip, whether
a GROUND leg exists between them. This is the §D taxonomy of issue #156, as amended
by the owner-decided C2 / W2: ground legs exist only at the ground endpoints of a
flight chain — the first departure and the last arrival — plus wherever a lodging
check-in (or evidence the operator left the terminal) breaks the chain.

Classification of a consecutive pair (N, N+1), in precedence order:

1. A lodging check-in sits between them → OVERNIGHT. Not a connection at all; it
   decomposes into a normal arrival leg (N → lodging) and a normal departure leg
   (lodging → N+1). Holds regardless of whether the airports match.
2. Different arrival/departure airports, no lodging between → DIFFERENT_AIRPORT_
   TRANSFER. The operator must physically cross town (LHR→LGW); a single transfer
   leg is generated. Silence here would strand him, so this ALWAYS generates
   (R3 different-airport rule).
3. Same airport, no lodging between → SAME_AIRPORT. Per the owner's C2 decision this
   DEFAULTS TO SILENCE (airside connection, no ground leg) at any gap length, and
   emits an arrival+departure pair ONLY on positive evidence the operator left the
   terminal (a fresh geofence fix outside the airport). The accepted residual is a
   narrow false-absent when a hotel is booked outside TripIt AND no fresh fix
   exists — silence, the safe failure for a layover.

The chain's FIRST departure is always ground-reached (its origin resolves via
position_at) and its LAST arrival always gets a drive-away leg. Interior
departures reached by a connecting flight (no lodging, no "left" evidence) are
suppressed — this is the defect behind the live `Drive: → CPH (SK915)` block,
where flight-assist routed a ground drive to an airport the operator reaches by a
prior flight.

Pure: the caller supplies, per pair, whether a lodging check-in falls between the
two flights and whether there is fresh evidence the operator left the terminal.
Lodging-schedule reads and geofence evaluation happen upstream; this module only
classifies and decides which legs a chain yields.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from flight_identity import MergedFlight


class ConnectionKind(Enum):
    """How a consecutive flight pair connects on the ground."""

    OVERNIGHT = "overnight"
    DIFFERENT_AIRPORT_TRANSFER = "different_airport_transfer"
    SAME_AIRPORT_LEFT = "same_airport_left"
    SAME_AIRPORT_CONNECTION = "same_airport_connection"


# Connection kinds that break the flight chain — i.e. a ground leg (or leg pair)
# exists between the two flights, so the earlier flight's arrival is a chain-final
# arrival for leg purposes and the later flight's departure is ground-reached.
_CHAIN_BREAKING = frozenset(
    {
        ConnectionKind.OVERNIGHT,
        ConnectionKind.DIFFERENT_AIRPORT_TRANSFER,
        ConnectionKind.SAME_AIRPORT_LEFT,
    }
)


@dataclass(frozen=True)
class PairContext:
    """Per-pair ground facts the classifier needs, resolved upstream.

    lodging_between — a lodging check-in falls in the gap between arrival of the
        earlier flight and departure of the later one.
    operator_left_terminal — fresh positive evidence (geofence fix outside the
        departure airport) that the operator left the terminal during the gap.
        Meaningful only for the same-airport case; ignored otherwise.
    """

    lodging_between: bool = False
    operator_left_terminal: bool = False


def classify_pair(earlier: MergedFlight, later: MergedFlight, ctx: PairContext) -> ConnectionKind:
    """Classify one consecutive flight pair. Precedence: lodging → airport → evidence."""
    if ctx.lodging_between:
        return ConnectionKind.OVERNIGHT
    if earlier.arr_airport != later.dep_airport:
        return ConnectionKind.DIFFERENT_AIRPORT_TRANSFER
    if ctx.operator_left_terminal:
        return ConnectionKind.SAME_AIRPORT_LEFT
    return ConnectionKind.SAME_AIRPORT_CONNECTION


class LegKind(Enum):
    """The ground legs a chain yields, before anchor/route/trivial resolution."""

    AIRPORT_DEPARTURE = "airport_departure"  # position_at(leave_by) → dep airport
    AIRPORT_ARRIVAL = "airport_arrival"  # arr airport → position_at(depart_after)
    AIRPORT_TRANSFER = "airport_transfer"  # arr(N) → dep(N+1), both fixed


@dataclass(frozen=True)
class PlannedLeg:
    """A ground leg the chain requires. Endpoints beyond the fixed airport(s) are
    resolved later via position_at; this records only WHICH legs exist and the
    flight(s) they attach to, with a stable identity for reconcile/dedupe.
    """

    kind: LegKind
    # The flight this leg departs from the ground toward (departure/transfer-dest)
    # and/or arrives from (arrival/transfer-origin). Transfer carries both.
    from_flight: MergedFlight | None = None
    to_flight: MergedFlight | None = None


def plan_chain_legs(chain: list[MergedFlight], contexts: list[PairContext]) -> list[PlannedLeg]:
    """Decide the ground legs for one ordered flight chain (#156 §D / C2 / W2).

    `chain` is the trip's flights in departure order; `contexts` holds the
    per-pair ground facts, so `len(contexts) == len(chain) - 1`. Returns the legs
    the chain requires — the chain-opening departure, the chain-closing arrival,
    any transfers, and the arrival/departure pairs that overnights and
    same-airport-left breaks introduce. Same-airport airside connections yield no
    leg. A single flight yields exactly a departure and an arrival.
    """
    if not chain:
        return []
    if len(contexts) != len(chain) - 1:
        raise ValueError(
            f"expected {len(chain) - 1} pair contexts for a {len(chain)}-flight chain, "
            f"got {len(contexts)}"
        )

    kinds = [classify_pair(chain[i], chain[i + 1], contexts[i]) for i in range(len(chain) - 1)]

    legs: list[PlannedLeg] = []

    # Chain-opening departure: the first flight's departure airport is always
    # reached by ground.
    legs.append(PlannedLeg(LegKind.AIRPORT_DEPARTURE, to_flight=chain[0]))

    for i, kind in enumerate(kinds):
        earlier, later = chain[i], chain[i + 1]
        if kind is ConnectionKind.SAME_AIRPORT_CONNECTION:
            continue  # airside — no ground leg either side of this seam
        if kind is ConnectionKind.DIFFERENT_AIRPORT_TRANSFER:
            legs.append(PlannedLeg(LegKind.AIRPORT_TRANSFER, from_flight=earlier, to_flight=later))
            continue
        # OVERNIGHT or SAME_AIRPORT_LEFT: the chain breaks — the earlier flight
        # gets a drive-away arrival leg and the later flight a ground-reached
        # departure leg, each resolved via position_at.
        legs.append(PlannedLeg(LegKind.AIRPORT_ARRIVAL, from_flight=earlier))
        legs.append(PlannedLeg(LegKind.AIRPORT_DEPARTURE, to_flight=later))

    # Chain-closing arrival: the last flight's arrival always gets a drive-away leg.
    legs.append(PlannedLeg(LegKind.AIRPORT_ARRIVAL, from_flight=chain[-1]))

    return legs


def is_chain_breaking(kind: ConnectionKind) -> bool:
    """Whether the pair introduces a ground leg (breaks the airside chain)."""
    return kind in _CHAIN_BREAKING
