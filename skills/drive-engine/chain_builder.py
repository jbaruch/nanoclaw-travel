"""Chain assembly — merged flights → ordered chains + per-pair context — pure.

Bridges `flight_identity.merge_flights` and `chain.plan_chain_legs`: groups the
merged flights into ordered per-trip chains and derives each consecutive pair's
`PairContext` (is there a lodging check-in between the two flights; did the operator
leave the terminal). Those two facts drive the §D / C2 connection classification.

Pure: the lodging fact is read from the itinerary schedule (already loaded); the
"left the terminal" fact is a geofence decision the I/O layer makes on the current
location fix and passes in as a predicate. No clock, no network here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from chain import PairContext
from flight_identity import MergedFlight


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def group_into_chains(flights: list[MergedFlight]) -> list[list[MergedFlight]]:
    """Group merged flights into ordered chains, one per trip.

    Flights sharing a `trip_id` form a chain, ordered by scheduled departure. A
    flight with no `trip_id` (e.g. a TripIt-only or byAir-only straggler) forms its
    own singleton chain. Chains are returned ordered by their first flight's
    scheduled departure, so the output is deterministic.
    """
    by_trip: dict[int, list[MergedFlight]] = {}
    singletons: list[list[MergedFlight]] = []
    for f in flights:
        if f.trip_id is None:
            singletons.append([f])
        else:
            by_trip.setdefault(f.trip_id, []).append(f)

    chains: list[list[MergedFlight]] = [
        sorted(group, key=lambda f: f.scheduled_dep) for group in by_trip.values()
    ]
    chains.extend(singletons)
    chains.sort(key=lambda chain: chain[0].scheduled_dep)
    return chains


def has_lodging_between(schedule: list[dict] | None, start: datetime, end: datetime) -> bool:
    """Whether a lodging check-in falls strictly within `(start, end)`.

    Scans the itinerary schedule for a `Lodging` record whose check-in instant is
    after `start` and before `end` — the discriminator between an overnight (a
    lodging break) and an airside connection (§D).
    """
    if start >= end:
        return False
    for record in schedule or []:
        if not isinstance(record, dict) or record.get("type") != "Lodging":
            continue
        when = _parse_iso(record.get("start"))
        if when is not None and start < when < end:
            return True
    return False


def build_pair_contexts(
    chain: list[MergedFlight],
    *,
    schedule: list[dict] | None,
    left_terminal: Callable[[MergedFlight, MergedFlight], bool] | None = None,
) -> list[PairContext]:
    """Derive the `PairContext` for each consecutive pair in a chain.

    `left_terminal(earlier, later)` returns fresh geofence evidence the operator
    left the departure airport during the gap (only consulted for same-airport
    pairs by the classifier; default no evidence). Uses each flight's best-known
    times: the earlier flight's arrival and the later flight's departure bound the
    gap the lodging check is run over.
    """
    contexts: list[PairContext] = []
    for earlier, later in zip(chain, chain[1:], strict=False):
        arr = earlier.effective_arr
        dep = later.effective_dep
        lodging = has_lodging_between(schedule, arr, dep) if arr is not None else False
        left = bool(left_terminal(earlier, later)) if left_terminal is not None else False
        contexts.append(PairContext(lodging_between=lodging, operator_left_terminal=left))
    return contexts
