"""Resolve the boarding-lead time (minutes before departure) for a flight.

The boarding block flight-assist creates spans [departure - lead, departure].
The lead encodes a boarding-pace policy that has changed more than once, so
it lives in this one isolated, unit-tested module rather than inside the
calendar planner. The planner consumes the resolved integer only.

Policy (in precedence order):

  1. Transoceanic crossing (transatlantic / transpacific)  -> 50 min,
     regardless of aircraft. Boarding an ocean-crossing flight starts
     earlier even on the rare narrowbody that flies it (A321LR, 757).
  2. Widebody (twin-aisle) aircraft                        -> 50 min.
  3. Narrowbody (single-aisle, regional, turboprop)        -> 30 min.
  4. Nothing classifiable (no aircraft model, no usable
     coordinates)                                          -> 30 min.

Aisle count is the split, not exact size: the A320 family (incl. A321),
all 737 variants, the 757, and regional jets/turboprops are narrowbody
(30); twin-aisle widebodies (A330/A340/A350/A380, 747/767/777/787, ...)
are 50.

Transoceanic detection is a longitude/distance heuristic — no airport
country/continent table. Each airport falls in a longitude block; an
Americas <-> Europe/Africa pair is transatlantic and an Americas <->
Asia/Oceania pair is transpacific, gated by a great-circle distance floor
so a short hop near a block boundary is not misread as an ocean crossing.
Europe <-> Asia long-haul (e.g. London-Singapore) is correctly NOT
transoceanic — it crosses land, not the Atlantic or Pacific.

stdlib-only (`math`) per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import math

LEAD_NARROWBODY_MINUTES = 30
LEAD_WIDEBODY_MINUTES = 50
LEAD_TRANSOCEANIC_MINUTES = 50
# No aircraft model and no usable coordinates: fall back to the narrowbody
# lead. Home base is domestic narrowbody; an unknown flight is far more
# likely a short narrowbody hop than a widebody or ocean crossing.
DEFAULT_LEAD_MINUTES = 30

# Substrings (matched against an uppercased, separator-stripped model
# string) that mark a twin-aisle widebody. Narrowbodies — A320 family
# incl. A321, every 737, the 757, regional jets, turboprops — carry none
# of these and resolve to narrowbody.
_WIDEBODY_TOKENS = (
    "A300",
    "A310",
    "A330",
    "A340",
    "A350",
    "A380",
    "747",
    "767",
    "777",
    "787",
    "IL96",
    "DC10",
    "MD11",
    "L1011",
)

# Great-circle distance floor for a longitude-block pair to count as an
# ocean crossing — filters short hops straddling a block boundary.
_TRANSOCEANIC_MIN_KM = 2000.0

SIZE_WIDEBODY = "widebody"
SIZE_NARROWBODY = "narrowbody"
SIZE_UNKNOWN = "unknown"


def classify_aircraft(model: str | None) -> str:
    """Classify an aircraft model string as widebody / narrowbody / unknown.

    Empty or None -> unknown (the caller falls back). Any model carrying a
    widebody token -> widebody. Every other non-empty model -> narrowbody
    (single-aisle and regional aircraft are the common case and the safe
    default once we know there IS an aircraft).
    """
    if not model:
        return SIZE_UNKNOWN
    normalized = "".join(ch for ch in model.upper() if ch.isalnum())
    for token in _WIDEBODY_TOKENS:
        if token in normalized:
            return SIZE_WIDEBODY
    return SIZE_NARROWBODY


def _lon_block(lon: float) -> str:
    """Bucket a longitude into a coarse ocean-bounding block.

    AMERICAS spans the western hemisphere airports; EURAFRICA spans Europe,
    Africa and the Middle East; ASIAOCEANIA spans the rest east to the date
    line (and its small wrap past -170 for the western Pacific islands).
    """
    if -170.0 <= lon <= -30.0:
        return "AMERICAS"
    if -30.0 < lon <= 60.0:
        return "EURAFRICA"
    return "ASIAOCEANIA"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def is_transoceanic(
    dep_lat: float, dep_lon: float, arr_lat: float, arr_lon: float
) -> bool:
    """True when the route crosses the Atlantic or Pacific (TATL / TPAC).

    Transatlantic = an Americas <-> Europe/Africa block pair; transpacific
    = an Americas <-> Asia/Oceania block pair. A same-block route, or a
    Europe/Africa <-> Asia/Oceania route (over land, not an ocean), is not
    transoceanic. Gated by `_TRANSOCEANIC_MIN_KM` so a short boundary-
    straddling hop does not register.
    """
    pair = frozenset({_lon_block(dep_lon), _lon_block(arr_lon)})
    transoceanic_pairs = (
        frozenset({"AMERICAS", "EURAFRICA"}),
        frozenset({"AMERICAS", "ASIAOCEANIA"}),
    )
    if pair not in transoceanic_pairs:
        return False
    return _haversine_km(dep_lat, dep_lon, arr_lat, arr_lon) >= _TRANSOCEANIC_MIN_KM


def resolve_boarding_lead_minutes(
    *,
    aircraft_model: str | None,
    inbound_aircraft_model: str | None,
    dep_lat: float | None,
    dep_lon: float | None,
    arr_lat: float | None,
    arr_lon: float | None,
) -> int:
    """Resolve the boarding lead in minutes for one flight.

    `aircraft_model` is byAir's top-level `model`; it falls back to
    `inbound_aircraft_model` (the Find My Plane chain) when empty, since
    `model` is sometimes blank. Coordinates are the dep/arr airport
    lat/lon; when any is missing the transoceanic check is skipped.

    Precedence: transoceanic (50) -> widebody (50) -> narrowbody (30) ->
    default (30). See the module docstring for the full policy.
    """
    if (
        dep_lat is not None
        and dep_lon is not None
        and arr_lat is not None
        and arr_lon is not None
        and is_transoceanic(dep_lat, dep_lon, arr_lat, arr_lon)
    ):
        return LEAD_TRANSOCEANIC_MINUTES

    size = classify_aircraft(aircraft_model or inbound_aircraft_model or "")
    if size == SIZE_WIDEBODY:
        return LEAD_WIDEBODY_MINUTES
    if size == SIZE_NARROWBODY:
        return LEAD_NARROWBODY_MINUTES
    return DEFAULT_LEAD_MINUTES
