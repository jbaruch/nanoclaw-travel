"""Traffic-aware travel time with a Google-primary, TomTom-backup chain.

Used by `phase_markers.py` to compute the "leave by" deadline for the
time-to-leave capability: given the user's current origin and the
flight's departure airport, return the duration with current traffic.
The same client is the shared ground-transit time source for the
`drive-engine` sweep (meetings and airports alike).

Routing chain (mission-critical — "I don't like to be late"):
    1. Google Distance Matrix (primary). Free-form origin/destination
       strings, returns duration with and without traffic in one call.
    2. TomTom (backup), used only when Google fails. TomTom routing is
       coordinates-only, so the backup is *geocode each endpoint →
       route between the coordinates*, not a drop-in. The resulting
       `TravelTime.source` is `"tomtom"` so callers can tell which
       provider answered.

There is deliberately NO no-traffic fallback (e.g. OSRM): a duration
without a live-traffic model is false confidence for a leave-by
deadline. When both providers fail, fail honestly with `MapsError`.

stdlib-only: `urllib.request` + `urllib.parse` + `json` per
`jbaruch/coding-policy: dependency-management` (Stdlib First).

Public API:
    # The skill bundle dir is added to sys.path at invocation time; this
    # module is imported by its bare name (matches nanoclaw-core's convention).
    from maps_client import MapsClient, MapsError, TravelTime

    client = MapsClient.from_env()  # wires the TomTom backup if TOMTOM_API_KEY is set
    result = client.travel_time(
        origin="1 Infinite Loop, Cupertino, CA",
        destination="SFO airport",
    )
    print(result.in_traffic_seconds, result.traffic_factor, result.source)

Errors:
    - `MapsError` wraps non-OK provider statuses (Google `NOT_FOUND`,
      `ZERO_RESULTS`, `OVER_QUERY_LIMIT`; TomTom geocode/route failures)
      and, when the whole chain is exhausted, status
      `ALL_PROVIDERS_FAILED` carrying what each provider reported
    - HTTP / network errors from a provider with no configured backup
      propagate as `urllib.error.URLError` / `urllib.error.HTTPError`
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

_GOOGLE_ENDPOINT = "https://maps.googleapis.com/maps/api/distancematrix/json"
_TOMTOM_GEOCODE_ENDPOINT = "https://api.tomtom.com/search/2/geocode"
_TOMTOM_ROUTE_ENDPOINT = "https://api.tomtom.com/routing/1/calculateRoute"


class MapsError(Exception):
    """Raised when a routing provider returns a non-OK status."""

    def __init__(self, status: str, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class TravelTime:
    """One origin→destination travel-time result from a routing provider.

    Fields:
        duration_seconds: Free-flow duration (no current-traffic model)
        in_traffic_seconds: Duration with current traffic conditions
            (None when the provider didn't return a traffic estimate).
            Use this for the time-to-leave deadline when present.
        traffic_factor: in_traffic / duration ratio (1.0 when no traffic
            data; >1.0 when current traffic slows travel)
        distance_meters: Distance between origin and destination
        origin_resolved: The address the provider matched the origin to
        destination_resolved: The address the provider matched the dest to
        source: Which provider answered — "google" or "tomtom"
    """

    duration_seconds: int
    in_traffic_seconds: int | None
    traffic_factor: float
    distance_meters: int
    origin_resolved: str
    destination_resolved: str
    source: str


def _get_json(url: str, timeout: float) -> dict:
    """GET a URL and decode the JSON body. Transport errors propagate."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _traffic_factor(duration: int, in_traffic: int | None) -> float:
    if in_traffic is not None and duration > 0:
        return in_traffic / duration
    return 1.0


class TomTomClient:
    """TomTom geocode + routing backup.

    TomTom routing takes coordinates, not free-form strings, so a single
    `travel_time()` is three HTTP calls: geocode the origin, geocode the
    destination, then route between the two coordinate pairs with
    `traffic=true`. Each call is billable against the TomTom free tier.
    """

    def __init__(self, api_key: str, *, timeout: float = 10.0):
        if not api_key:
            raise ValueError(
                "TomTomClient: api_key is empty — set TOMTOM_API_KEY in the env "
                "(create a free key at https://developer.tomtom.com/)"
            )
        self._api_key = api_key
        self._timeout = timeout

    @classmethod
    def from_env(cls, *, env_var: str = "TOMTOM_API_KEY", timeout: float = 10.0) -> TomTomClient:
        """Construct from the TOMTOM_API_KEY env var."""
        api_key = os.environ.get(env_var, "")
        if not api_key:
            raise ValueError(
                f"TomTomClient.from_env: ${env_var} is unset — create a free key at "
                f"https://developer.tomtom.com/ and add it to OneCLI vault"
            )
        return cls(api_key, timeout=timeout)

    def travel_time(self, origin: str, destination: str) -> TravelTime:
        """Geocode both endpoints, then route between them with live traffic.

        Raises:
            ValueError: if either argument is empty
            MapsError: on geocode zero-results or a malformed route response
            urllib.error.HTTPError: on transport failure
        """
        if not origin or not destination:
            raise ValueError("travel_time: origin and destination are required")

        origin_lat, origin_lon, origin_resolved = self._geocode(origin)
        dest_lat, dest_lon, dest_resolved = self._geocode(destination)
        return self._route(
            (origin_lat, origin_lon, origin_resolved),
            (dest_lat, dest_lon, dest_resolved),
        )

    def _geocode(self, query: str) -> tuple[float, float, str]:
        encoded = urllib.parse.quote(query, safe="")
        params = urllib.parse.urlencode({"key": self._api_key, "limit": 1})
        url = f"{_TOMTOM_GEOCODE_ENDPOINT}/{encoded}.json?{params}"
        payload = _get_json(url, self._timeout)

        results = payload.get("results") or []
        if not results:
            raise MapsError(
                "TOMTOM_GEOCODE_ZERO_RESULTS",
                f"TomTom geocode returned no match for {query!r}",
            )
        position = results[0].get("position") or {}
        lat = position.get("lat")
        lon = position.get("lon")
        if lat is None or lon is None:
            raise MapsError(
                "TOMTOM_MALFORMED_RESPONSE",
                f"TomTom geocode result missing lat/lon for {query!r}",
            )
        resolved = (results[0].get("address") or {}).get("freeformAddress") or query
        return float(lat), float(lon), resolved

    def _route(
        self,
        origin: tuple[float, float, str],
        destination: tuple[float, float, str],
    ) -> TravelTime:
        origin_lat, origin_lon, origin_resolved = origin
        dest_lat, dest_lon, dest_resolved = destination
        path = f"{origin_lat},{origin_lon}:{dest_lat},{dest_lon}"
        params = urllib.parse.urlencode(
            {
                "key": self._api_key,
                "traffic": "true",
                "travelMode": "car",
                "computeTravelTimeFor": "all",
            }
        )
        url = f"{_TOMTOM_ROUTE_ENDPOINT}/{path}/json?{params}"
        payload = _get_json(url, self._timeout)

        routes = payload.get("routes") or []
        if not routes:
            raise MapsError(
                "TOMTOM_ZERO_RESULTS",
                f"TomTom found no route from {origin_resolved} → {dest_resolved}",
            )
        summary = routes[0].get("summary") or {}
        travel_time = summary.get("travelTimeInSeconds")
        distance = summary.get("lengthInMeters")
        if travel_time is None or distance is None:
            raise MapsError(
                "TOMTOM_MALFORMED_RESPONSE",
                "TomTom route summary missing travelTimeInSeconds or lengthInMeters",
            )

        # With traffic=true, travelTimeInSeconds already includes the live
        # delay; computeTravelTimeFor=all adds the free-flow baseline so we
        # can report both, matching the Google duration / in_traffic split.
        free_flow = summary.get("noTrafficTravelTimeInSeconds")
        if free_flow is not None:
            duration = int(free_flow)
            in_traffic: int | None = int(travel_time)
        else:
            duration = int(travel_time)
            in_traffic = None

        return TravelTime(
            duration_seconds=duration,
            in_traffic_seconds=in_traffic,
            traffic_factor=_traffic_factor(duration, in_traffic),
            distance_meters=int(distance),
            origin_resolved=origin_resolved,
            destination_resolved=dest_resolved,
            source="tomtom",
        )


class MapsClient:
    """Google Distance Matrix primary with an optional TomTom backup.

    Each `travel_time()` call is one billable Google Distance Matrix
    request (and, only on Google failure, up to three TomTom requests).
    Cache aggressively at the caller level — for the time-to-leave
    capability, one or two queries per flight per day is the target.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 10.0,
        tomtom: TomTomClient | None = None,
    ):
        if not api_key:
            raise ValueError(
                "MapsClient: api_key is empty — set GOOGLE_MAPS_API_KEY in the env "
                "(generate at https://console.cloud.google.com/apis/credentials and "
                "enable the Distance Matrix API)"
            )
        self._api_key = api_key
        self._timeout = timeout
        self._tomtom = tomtom

    @classmethod
    def from_env(
        cls,
        *,
        env_var: str = "GOOGLE_MAPS_API_KEY",
        tomtom_env_var: str = "TOMTOM_API_KEY",
        timeout: float = 10.0,
    ) -> MapsClient:
        """Construct from GOOGLE_MAPS_API_KEY, wiring the TomTom backup.

        The TomTom backup is wired only when TOMTOM_API_KEY is set; absent
        it, the client runs Google-only and a Google failure propagates.
        """
        api_key = os.environ.get(env_var, "")
        if not api_key:
            raise ValueError(
                f"MapsClient.from_env: ${env_var} is unset — generate a Distance Matrix "
                f"API key at https://console.cloud.google.com/apis/credentials and add "
                f"it to OneCLI vault"
            )
        tomtom = None
        if os.environ.get(tomtom_env_var):
            tomtom = TomTomClient.from_env(env_var=tomtom_env_var, timeout=timeout)
        return cls(api_key, timeout=timeout, tomtom=tomtom)

    def travel_time(self, origin: str, destination: str) -> TravelTime:
        """Return travel time for one origin→destination pair.

        Tries Google first; on any Google `MapsError` or transport
        failure, falls back to TomTom when a backup is configured. Per
        `coding-policy: error-handling` "Graceful Fallback": when both
        providers fail, raises `MapsError("ALL_PROVIDERS_FAILED", ...)`
        naming what each provider reported.

        Raises:
            ValueError: if either argument is empty
            MapsError: on Google failure with no backup, or both failing
            urllib.error.HTTPError: on Google transport failure with no backup
        """
        if not origin or not destination:
            raise ValueError("travel_time: origin and destination are required")

        try:
            return self._google_travel_time(origin, destination)
        except (MapsError, urllib.error.URLError) as google_err:
            if self._tomtom is None:
                raise
            try:
                return self._tomtom.travel_time(origin, destination)
            except (MapsError, urllib.error.URLError) as tomtom_err:
                raise MapsError(
                    "ALL_PROVIDERS_FAILED",
                    f"Google failed ({google_err}); TomTom backup failed ({tomtom_err})",
                ) from tomtom_err

    def _google_travel_time(self, origin: str, destination: str) -> TravelTime:
        """Query the Google Distance Matrix API for a single pair.

        Uses `departure_time=now` + `traffic_model=best_guess` so the
        response includes a current-traffic estimate.
        """
        payload = _get_json(self._build_url(origin, destination), self._timeout)
        return self._parse(payload)

    def _build_url(self, origin: str, destination: str) -> str:
        query = urllib.parse.urlencode(
            {
                "origins": origin,
                "destinations": destination,
                "departure_time": "now",
                "traffic_model": "best_guess",
                "key": self._api_key,
            }
        )
        return f"{_GOOGLE_ENDPOINT}?{query}"

    def _parse(self, payload: dict) -> TravelTime:
        """Decode the Distance Matrix response into a TravelTime."""
        top_status = payload.get("status", "UNKNOWN")
        if top_status != "OK":
            raise MapsError(
                top_status,
                payload.get("error_message", "Distance Matrix request failed"),
            )

        origins = payload.get("origin_addresses") or [""]
        destinations = payload.get("destination_addresses") or [""]
        rows = payload.get("rows") or []
        if not rows or not rows[0].get("elements"):
            raise MapsError(
                "MALFORMED_RESPONSE",
                "Distance Matrix response had no elements",
            )

        element = rows[0]["elements"][0]
        element_status = element.get("status", "UNKNOWN")
        if element_status != "OK":
            raise MapsError(
                element_status,
                f"Element status from {origins[0]} → {destinations[0]}",
            )

        duration = element.get("duration", {}).get("value")
        distance = element.get("distance", {}).get("value")
        if duration is None or distance is None:
            raise MapsError(
                "MALFORMED_RESPONSE",
                "Distance Matrix element missing duration or distance",
            )

        in_traffic = None
        in_traffic_block = element.get("duration_in_traffic")
        if in_traffic_block and "value" in in_traffic_block:
            in_traffic = int(in_traffic_block["value"])

        return TravelTime(
            duration_seconds=int(duration),
            in_traffic_seconds=in_traffic,
            traffic_factor=_traffic_factor(int(duration), in_traffic),
            distance_meters=int(distance),
            origin_resolved=origins[0],
            destination_resolved=destinations[0],
            source="google",
        )
