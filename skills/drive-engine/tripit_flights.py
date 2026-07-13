"""TripIt Flight-segment → Flight normalization for the byAir ∪ TripIt union (R2).

The live sweep's flights come from byAir, but #156 R2 wants the UNION: a flight
tracked by EITHER source produces legs, so a TripIt segment byAir never ingested
(manual booking, unparsed email) is not silently dropped. `merge_flights` collapses
a byAir flight and its TripIt twin by (route, scheduled instant), so feeding both is
safe — duplicates fuse, singletons survive.

TripIt's travel-schedule Flight segments carry airports only in free text — the iCal
`[Flight] ATL to SJO` shape (regular, code-based), plus a designator in the summary.
That is bounded enough to parse (two IATA codes around `to`); a segment that doesn't
match is skipped rather than guessed, so a malformed row never fabricates a flight.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from flight_identity import Flight  # noqa: E402
from normalize import flight_from_tripit_segment  # noqa: E402

# TripIt iCal renders a flight route as "<DEP> to <ARR>" with 3-letter IATA codes,
# e.g. "[Flight] ATL to SJO" (description) or "DL 4908 ATL to SJO" (summary).
_ROUTE_RE = re.compile(r"\b([A-Z]{3})\s+to\s+([A-Z]{3})\b")
# A leading IATA flight designator in the summary, e.g. "DL 4908", "FR7382".
_CODE_RE = re.compile(r"\b([A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(\d{1,4})\b")


def _route(segment: dict) -> tuple[str, str] | None:
    for field in ("description", "summary"):
        text = segment.get(field)
        if isinstance(text, str):
            m = _ROUTE_RE.search(text)
            if m:
                return m.group(1), m.group(2)
    return None


def _code(segment: dict) -> str | None:
    summary = segment.get("summary")
    if isinstance(summary, str):
        m = _CODE_RE.search(summary)
        if m:
            return f"{m.group(1)}{m.group(2)}"
    return None


def flights_from_schedule(schedule: list[dict] | None) -> list[Flight]:
    """Normalize the schedule's `Flight` segments into TripIt-source `Flight`s.

    Only segments with a parseable route AND a parseable start become flights; the
    rest are skipped (never guessed). Airports are the IATA codes from the route
    text; times from `start` / `end`.
    """
    flights: list[Flight] = []
    for segment in schedule or []:
        if not isinstance(segment, dict) or segment.get("type") != "Flight":
            continue
        route = _route(segment)
        if route is None:
            continue
        dep_iata, arr_iata = route
        try:
            flights.append(
                flight_from_tripit_segment(
                    segment, dep_iata=dep_iata, arr_iata=arr_iata, code=_code(segment)
                )
            )
        except ValueError:
            # Missing uid / unparseable start — skip, don't fabricate a flight.
            continue
    return flights
