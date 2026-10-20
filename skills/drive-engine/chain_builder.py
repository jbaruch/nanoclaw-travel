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
from datetime import UTC, datetime

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
    """Group merged flights into operational chains by preferred `trip_id`.

    The preferred id preserves byAir's intentional split between outbound and
    return journeys around a long stay. Feeding a shared TripIt itinerary alias
    into connection classification would misread the stay as an airside layover
    when no lodging record exists. A flight with no preferred id forms its own
    singleton chain. Chains are ordered by their first scheduled departure.
    """
    by_trip: dict[int, list[MergedFlight]] = {}
    singletons: list[list[MergedFlight]] = []
    for flight in flights:
        if flight.trip_id is None:
            singletons.append([flight])
        else:
            by_trip.setdefault(flight.trip_id, []).append(flight)

    chains = [sorted(group, key=lambda flight: flight.scheduled_dep) for group in by_trip.values()]
    chains.extend(singletons)
    chains.sort(key=lambda chain: chain[0].scheduled_dep)
    return chains


def _spans_overnight(start: datetime, end: datetime) -> bool:
    """Whether a connection gap crosses a calendar night (different UTC days).

    A real overnight break spans midnight; a same-day layover does not. TripIt
    records a hotel's check-in / check-out at a NOMINAL local time (a standard
    mid-afternoon check-in), so a DESTINATION hotel's check-in can land inside a
    long SAME-DAY layover window even though that hotel is reached only by a later
    flight — a Tel Aviv 12:00Z (15:00 local) check-in landing in a 06:40Z–14:25Z
    Paris (CDG) layover, misread as an overnight, drew a bogus `Drive: CDG → Tel
    Aviv` block. The same-day gate drops that false break; every genuine
    cross-night stay (the real Tel Aviv stay spans Aug 2 → Aug 6) still crosses a
    day boundary and stands.
    """
    return start.astimezone(UTC).date() != end.astimezone(UTC).date()


def has_lodging_between(schedule: list[dict] | None, start: datetime, end: datetime) -> bool:
    """Whether a lodging check-in falls strictly within `(start, end)`.

    Scans the itinerary schedule for a `Lodging` record whose check-in instant is
    after `start` and before `end`. Necessary but not sufficient for an overnight
    break — `build_pair_contexts` also requires the gap to span a night (see
    `_spans_overnight`) before classifying the pair as an overnight (§D).
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

    `lodging_between` is set only when a lodging check-in falls in that gap AND the
    gap spans a night (`_spans_overnight`) — a same-day layover can't be an
    overnight however a nominal destination-hotel check-in time lands in it.
    """
    contexts: list[PairContext] = []
    for earlier, later in zip(chain, chain[1:], strict=False):
        arr = earlier.effective_arr
        dep = later.effective_dep
        lodging = bool(
            arr is not None
            and dep is not None
            and _spans_overnight(arr, dep)
            and has_lodging_between(schedule, arr, dep)
        )
        left = bool(left_terminal(earlier, later)) if left_terminal is not None else False
        contexts.append(PairContext(lodging_between=lodging, operator_left_terminal=left))
    return contexts
