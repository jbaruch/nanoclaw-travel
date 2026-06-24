"""A single drive-planner-owned routing-failure type — the route boundary.

The sweep planner and the recheck poll both price legs through an injected
`route(origin, destination)` callable so the pure decision cores stay decoupled
from `maps_client` (and CI-testable without it). Those cores need to catch a
routing failure — to record it as a `route_error` and move on — without a bare
`except Exception` (per `coding-policy: error-handling`, Specific Exceptions).
`RouteError` is that specific type: the live `_route_seconds` wrapper translates
the provider's `MapsError` / `urllib` transport errors into a `RouteError`, and
the pure cores catch only `RouteError`. A non-routing bug (e.g. a leg with no
anchor) is not a `RouteError` and propagates, as the rule requires.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).
"""

from __future__ import annotations


class RouteError(Exception):
    """A leg could not be priced — the routing provider failed for this pair.

    Raised by the route wrapper after the provider's `MapsError` / transport
    error; caught by the sweep planner and recheck poll to record an un-priced
    leg rather than abort the batch. Carries the original message.
    """
