"""Memoized read-only Maps routes shared by travel callers; no state or cadence."""

import re
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))
_FLIGHT_ASSIST = Path("/home/node/.claude/skills/tessl__flight-assist")
if not _FLIGHT_ASSIST.is_dir():
    _FLIGHT_ASSIST = _BUNDLE_DIR.parent / "flight-assist"
if str(_FLIGHT_ASSIST) not in sys.path:
    sys.path.insert(0, str(_FLIGHT_ASSIST))

RouteFn = Callable[[str, str], "timedelta | None"]
_TEXT_AIRPORT = re.compile(r"[A-Z]{3} airport")


def make_route(
    maps,
    *,
    cache: dict[tuple[str, str], timedelta | None] | None = None,
    distances: dict[tuple[str, str], float] | None = None,
    no_routes: set[tuple[str, str]] | None = None,
) -> RouteFn:
    """A memoizing `route(origin, destination) -> timedelta | None`.

    Per `MapsClient`'s own contract ("cache aggressively at the caller level"),
    dedupes identical (origin, destination) pairs within one sweep — an airport
    that is both a departure destination and a transfer origin is routed once, not
    per leg. Traffic is stable across a single sweep, so a cached duration is the
    same answer the provider would return again (#172). A failed route caches None
    too, so a dead endpoint isn't re-attempted every leg.

    There is no routing deadline: the plan phase runs to completion (#211).
    Runaway routing is bounded instead by the maps client's per-call timeout
    — a hung provider fails the single leg (cached
    as None) rather than stalling the sweep.
    """
    import urllib.error

    from maps_client import MapsError

    memo: dict[tuple[str, str], timedelta | None] = {} if cache is None else cache

    def route(origin: str, destination: str) -> timedelta | None:
        key = (origin, destination)
        if key in memo:
            return memo[key]
        try:
            tt = maps.travel_time(origin, destination)
        except (MapsError, urllib.error.URLError, TimeoutError) as exc:
            if (
                no_routes is not None
                and isinstance(exc, MapsError)
                and exc.status in {"ZERO_RESULTS", "NO_ROUTE"}
            ):
                no_routes.add(key)
            memo[key] = None
            return None
        if tt.source != "google" and (
            _TEXT_AIRPORT.fullmatch(origin) or _TEXT_AIRPORT.fullmatch(destination)
        ):
            # The backup geocoded "SFO airport" as the town of Airport, MO (#334).
            # A text airport is trusted from the primary only; the leg is held.
            print(
                f"[drive-engine] route {origin} → {destination}: {tt.source} answer for a "
                f"text airport ({tt.origin_resolved} → {tt.destination_resolved}) refused",
                file=sys.stderr,
            )
            memo[key] = None
            return None
        seconds = (
            tt.in_traffic_seconds if tt.in_traffic_seconds is not None else tt.duration_seconds
        )
        result = timedelta(seconds=seconds)
        if distances is not None:
            from ev_charging import finite

            if finite(tt.distance_meters, 0, 100000000):
                distances[key] = tt.distance_meters / 1000
        memo[key] = result
        return result

    return route
