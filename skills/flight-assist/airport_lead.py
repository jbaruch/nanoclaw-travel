"""Resolve airport clearance time — how early to be at the airport, and how
long after landing before the drive home can start.

Sibling of `boarding_lead.py`: an isolated, unit-tested policy module the
calendar planner consumes as resolved integers. Where `boarding_lead`
encodes boarding *pace* (the [dep-lead, dep] block), this encodes the
ground-transit *deadlines* around a flight:

  - Departure clearance — minutes before scheduled departure the traveller
    must already be AT the airport (security, and exit immigration where it
    applies). The drive-to-airport block ends at `dep - clearance`.
  - Post-arrival delay — minutes after actual landing before the traveller
    can leave the curb (deplane + bag; plus immigration + customs on an
    international arrival). The drive-home block starts at `arr + delay`.

Determinism note (per `jbaruch/coding-policy: script-delegation`): byAir
exposes NO structured "security wait" or "recommended arrival" field — only
a per-airport congestion `delay.index` and free-text community tips. The
base buffers here are an operator policy table; the congestion index nudges
the departure buffer; the free-text tips are an agent (reasoning-layer)
concern and are NOT consumed here.

International vs domestic is decided from the airports' countries. byAir
exposes no ISO country code — only `countryName` (native spelling, e.g.
`Türkiye`, `Czechia`) and a `countryFlag` emoji. The flag emoji IS two
Unicode regional-indicator codepoints that map 1:1 onto the ISO 3166-1
alpha-2 code, so `flag_to_iso` decodes it and the classification matches a
canonical code set — sidestepping byAir's spelling quirks entirely.

Intra-Schengen flights cross a border but pass no passport/customs control,
so they count as DOMESTIC on both the departure and arrival side.

stdlib-only (no imports) per `jbaruch/coding-policy: dependency-management`.
"""

from __future__ import annotations

# --- Operator policy table (config-overridable at the call site) -------
# These are the confirmed defaults; the calendar planner may override them
# from `config.json`. Minutes.
BASE_CLEARANCE_DOMESTIC_MINUTES = 60
BASE_CLEARANCE_INTERNATIONAL_MINUTES = 120

# Post-arrival delay before the drive home can start, by what control awaits
# on landing.
POST_ARRIVAL_DOMESTIC_MINUTES = 20  # deplane + bag, no immigration
POST_ARRIVAL_INTL_TO_US_MINUTES = 40  # US immigration + bag + customs
POST_ARRIVAL_INTL_ABROAD_MINUTES = 60  # non-US immigration + bag + customs

# Departure-clearance nudge keyed on byAir's airport `delay.index`. A
# congested departure airport means longer security lines, so be there
# earlier. Any other / missing index contributes 0.
DELAY_NUDGE_MINUTES = {
    "low": 0,
    "medium": 15,
    "high": 30,
}

# ISO 3166-1 alpha-2 codes of the 29 Schengen members, as of 2026-01
# (Bulgaria/Romania full members since 2025-01, Croatia since 2023).
# Cyprus (CY) is an EU member but not yet in Schengen. Maintained set —
# revisit on accession changes.
SCHENGEN = frozenset(
    "AT BE BG HR CZ DK EE FI FR DE GR HU IS IT LV LI LT LU MT NL NO PL PT RO SK SI ES SE CH".split()
)

# byAir's country string / decoded ISO for the United States.
US_ISO = "US"

# Classification labels.
CLASS_DOMESTIC = "domestic"
CLASS_INTERNATIONAL = "international"
ARRIVAL_DOMESTIC = "domestic"
ARRIVAL_INTL_TO_US = "intl_to_us"
ARRIVAL_INTL_ABROAD = "intl_abroad"

# Unicode regional-indicator block: U+1F1E6 ('A') .. U+1F1FF ('Z').
_RI_BASE = 0x1F1E6
_RI_LAST = 0x1F1FF


def flag_to_iso(flag: str | None) -> str | None:
    """Decode a regional-indicator flag emoji to its ISO 3166-1 alpha-2 code.

    `"🇨🇿"` -> `"CZ"`. Returns None unless the string is *exactly* two
    regional-indicator symbols and nothing else — surrounding whitespace,
    stray characters, a lone indicator, or tag-sequence flags (the
    England/Scotland subdivision flags) all reject. Strictness is the safe
    direction: a malformed flag yields None and the caller over-buffers
    (international), rather than silently decoding to a domestic country.
    """
    if not flag or len(flag) != 2:
        return None
    codepoints = [ord(ch) for ch in flag]
    if not all(_RI_BASE <= cp <= _RI_LAST for cp in codepoints):
        return None
    return "".join(chr(cp - _RI_BASE + ord("A")) for cp in codepoints)


def departure_class(dep_iso: str | None, arr_iso: str | None) -> str:
    """Classify the departure side of a flight as domestic or international.

    Domestic when both endpoints are the same country, OR both are Schengen
    members (intra-Schengen crosses no control border). International
    otherwise. An undecodable endpoint (None) is treated as international —
    over-buffering an airport run is safe; under-buffering risks the flight.
    """
    if dep_iso is None or arr_iso is None:
        return CLASS_INTERNATIONAL
    if dep_iso == arr_iso:
        return CLASS_DOMESTIC
    if dep_iso in SCHENGEN and arr_iso in SCHENGEN:
        return CLASS_DOMESTIC
    return CLASS_INTERNATIONAL


def arrival_class(dep_iso: str | None, arr_iso: str | None) -> str:
    """Classify what control awaits on landing: domestic, intl-to-US, abroad.

    Domestic (incl. intra-Schengen) clears fastest. An international arrival
    INTO the US and an international arrival abroad get distinct delays.
    """
    if departure_class(dep_iso, arr_iso) == CLASS_DOMESTIC:
        return ARRIVAL_DOMESTIC
    if arr_iso == US_ISO:
        return ARRIVAL_INTL_TO_US
    return ARRIVAL_INTL_ABROAD


def resolve_departure_clearance_minutes(
    *,
    dep_flag: str | None,
    arr_flag: str | None,
    delay_index: str | None,
    domestic_minutes: int = BASE_CLEARANCE_DOMESTIC_MINUTES,
    international_minutes: int = BASE_CLEARANCE_INTERNATIONAL_MINUTES,
) -> int:
    """Minutes before scheduled departure to be AT the airport.

    `dep_flag` / `arr_flag` are byAir's `countryFlag` emoji for the
    departure and arrival airports; `delay_index` is byAir's airport
    `delay.index` for the DEPARTURE airport (`"low"`/`"medium"`/`"high"`).
    The base buffer is the route class; the congestion index nudges it up.
    `*_minutes` let the calendar planner override the policy from config.
    """
    cls = departure_class(flag_to_iso(dep_flag), flag_to_iso(arr_flag))
    base = domestic_minutes if cls == CLASS_DOMESTIC else international_minutes
    nudge = DELAY_NUDGE_MINUTES.get((delay_index or "").lower(), 0)
    return base + nudge


def resolve_post_arrival_minutes(
    *,
    dep_flag: str | None,
    arr_flag: str | None,
    domestic_minutes: int = POST_ARRIVAL_DOMESTIC_MINUTES,
    intl_to_us_minutes: int = POST_ARRIVAL_INTL_TO_US_MINUTES,
    intl_abroad_minutes: int = POST_ARRIVAL_INTL_ABROAD_MINUTES,
) -> int:
    """Minutes after actual landing before the drive home can start.

    `dep_flag` / `arr_flag` are byAir's `countryFlag` emoji for the
    departure and arrival airports. No congestion nudge applies on the
    arrival side. `*_minutes` let the calendar planner override from config.
    """
    cls = arrival_class(flag_to_iso(dep_flag), flag_to_iso(arr_flag))
    if cls == ARRIVAL_DOMESTIC:
        return domestic_minutes
    if cls == ARRIVAL_INTL_TO_US:
        return intl_to_us_minutes
    return intl_abroad_minutes
