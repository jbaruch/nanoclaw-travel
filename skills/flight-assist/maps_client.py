"""Google Maps Distance Matrix client for traffic-aware travel time.

Used by `phase_markers.py` to compute the "leave by" deadline for the
time-to-leave capability: given the user's current origin and the
flight's departure airport, return the duration with current traffic.

The Distance Matrix API endpoint takes free-form origin/destination
strings (addresses, place names, "SFO airport"). Returns the duration
both with and without traffic; the precheck uses the in-traffic value
for the time-to-leave deadline.

stdlib-only: `urllib.request` + `urllib.parse` + `json` per
`jbaruch/coding-policy: dependency-management` (Stdlib First).

Public API:
    # The skill bundle dir is added to sys.path at invocation time; this
    # module is imported by its bare name (matches nanoclaw-core's convention).
    from maps_client import MapsClient, MapsError, TravelTime

    client = MapsClient.from_env()
    result = client.travel_time(
        origin="1 Infinite Loop, Cupertino, CA",
        destination="SFO airport",
    )
    print(result.in_traffic_seconds, result.traffic_factor)

Errors:
    - `MapsError` wraps non-OK API statuses (`NOT_FOUND`,
      `ZERO_RESULTS`, `OVER_QUERY_LIMIT`, etc.) per the Distance Matrix
      response's `status` and per-element `status` fields
    - HTTP / network errors propagate as `urllib.error.URLError` /
      `urllib.error.HTTPError`
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

_API_ENDPOINT = "https://maps.googleapis.com/maps/api/distancematrix/json"


class MapsError(Exception):
    """Raised when Google Maps Distance Matrix returns a non-OK status."""

    def __init__(self, status: str, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class TravelTime:
    """One origin→destination travel-time result from the Distance Matrix API.

    Fields:
        duration_seconds: Free-flow duration (no current-traffic model)
        in_traffic_seconds: Duration with current traffic conditions
            (None when the API didn't return a traffic estimate — e.g.,
            walking/transit modes or when traffic data isn't available).
            Use this for the time-to-leave deadline when present.
        traffic_factor: in_traffic / duration ratio (1.0 when no traffic
            data; >1.0 when current traffic slows travel)
        distance_meters: Distance between origin and destination
        origin_resolved: The address Google matched the origin string to
        destination_resolved: The address Google matched the destination to
    """

    duration_seconds: int
    in_traffic_seconds: int | None
    traffic_factor: float
    distance_meters: int
    origin_resolved: str
    destination_resolved: str


class MapsClient:
    """Distance Matrix API client.

    Each `travel_time()` call is one billable Distance Matrix request
    (Google pricing). Cache aggressively at the caller level — for the
    time-to-leave capability, one or two queries per flight per day
    is the target cadence.
    """

    def __init__(self, api_key: str, *, timeout: float = 10.0):
        if not api_key:
            raise ValueError(
                "MapsClient: api_key is empty — set GOOGLE_MAPS_API_KEY in the env "
                "(generate at https://console.cloud.google.com/apis/credentials and "
                "enable the Distance Matrix API)"
            )
        self._api_key = api_key
        self._timeout = timeout

    @classmethod
    def from_env(cls, *, env_var: str = "GOOGLE_MAPS_API_KEY", timeout: float = 10.0) -> MapsClient:
        """Construct from the GOOGLE_MAPS_API_KEY env var."""
        api_key = os.environ.get(env_var, "")
        if not api_key:
            raise ValueError(
                f"MapsClient.from_env: ${env_var} is unset — generate a Distance Matrix "
                f"API key at https://console.cloud.google.com/apis/credentials and add "
                f"it to OneCLI vault"
            )
        return cls(api_key, timeout=timeout)

    def travel_time(self, origin: str, destination: str) -> TravelTime:
        """Query the Distance Matrix API for a single origin→destination pair.

        Uses `departure_time=now` + `traffic_model=best_guess` so the
        response includes a current-traffic estimate. Returns a
        `TravelTime` dataclass.

        Raises:
            ValueError: if either argument is empty
            MapsError: on non-OK top-level or per-element status
            urllib.error.HTTPError: on transport failure
        """
        if not origin or not destination:
            raise ValueError("travel_time: origin and destination are required")

        url = self._build_url(origin, destination)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))

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
        return f"{_API_ENDPOINT}?{query}"

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
        traffic_factor = 1.0
        in_traffic_block = element.get("duration_in_traffic")
        if in_traffic_block and "value" in in_traffic_block:
            in_traffic = int(in_traffic_block["value"])
            if duration > 0:
                traffic_factor = in_traffic / duration

        return TravelTime(
            duration_seconds=int(duration),
            in_traffic_seconds=in_traffic,
            traffic_factor=traffic_factor,
            distance_meters=int(distance),
            origin_resolved=origins[0],
            destination_resolved=destinations[0],
        )
