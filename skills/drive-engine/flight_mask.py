"""Identity-based flight mask for the meeting source — pure, no I/O, no clock.

In the unified model the calendar is an OUTPUT for airport legs (those come from
the itinerary source, §C), and is read only for GENUINE ground meetings. Google
"events from Gmail", TripIt, and Flighty still auto-create flight events on that
calendar, so the meeting source must recognize and drop them.

Per #156 review R5 the mask suppresses ONLY by IDENTITY, never by time overlap:

- a flight-template summary — `✈…` or `Flight to …` — intrinsic, needs no schedule,
  so it catches duplicate / TZ-corrupt Gmail flight copies whose time is garbage;
- an IATA designator in the summary matching a KNOWN flight from the itinerary
  union — identity, not instant, so it survives a corrupted time.

Time overlap is explicitly NOT a suppressor here. A genuine ground meeting that
happens to overlap a redeye flight window must survive — associating byAir live
times to a flight window is a separate concern that never removes a meeting. This
deletes the old drive-planner 3-signal `_is_flight_event` (whose time-overlap
signal both mis-fired on TZ-corrupt copies and risked masking real meetings).

The caller builds `known_codes` from the RAW flight designators across both
sources (every code, pre-merge) so a calendar event labelled with either half of a
codeshare (FR 7382 / MW 7382) is still recognized, even though the canonical flight
identity itself excludes the designator.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# IATA-style flight designator: a 2-char carrier code (LL / L# / #L) followed by
# 1–4 digits, optional space. Matches "DL 4908", "FR7382", "U2 8001".
_FLIGHT_CODE_RE = re.compile(r"\b([A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(\d{1,4})\b")

# Intrinsic flight-template summary markers. Flighty prefixes "✈"; Google/TripIt
# flight auto-events read "Flight to <city>".
_FLIGHT_SUMMARY_PREFIXES = ("flight to ", "✈")


def normalize_code(code: str) -> str:
    """Canonicalize a designator for comparison: upper, no interior whitespace."""
    return re.sub(r"\s+", "", code).upper()


def flight_codes(summary: str | None) -> set[str]:
    """Every IATA designator in a summary, normalized. Empty when none / None."""
    if not summary:
        return set()
    return {normalize_code(f"{a}{b}") for a, b in _FLIGHT_CODE_RE.findall(summary.upper())}


def known_flight_codes(codes: Iterable[str | None]) -> set[str]:
    """Build the known-designator set from raw flight codes (both sources).

    Accepts any iterable of code strings (e.g. every `Flight.code` before merge).
    Skips falsy / non-string entries and normalizes the rest.
    """
    result: set[str] = set()
    for code in codes:
        if isinstance(code, str) and code.strip():
            result.add(normalize_code(code))
    return result


def looks_like_flight_summary(summary: str | None) -> bool:
    """Whether the summary is an intrinsic flight template (`✈…` / `Flight to …`)."""
    if not summary:
        return False
    text = summary.strip().lower()
    return any(text.startswith(prefix) for prefix in _FLIGHT_SUMMARY_PREFIXES)


def is_flight_event(summary: str | None, known_codes: set[str]) -> bool:
    """Whether a calendar event is air travel, by IDENTITY only (#156 R5).

    True when the summary is a flight template OR carries a designator matching a
    known itinerary flight. Never consults time — a ground meeting overlapping a
    flight window is not masked.
    """
    if looks_like_flight_summary(summary):
        return True
    return bool(flight_codes(summary) & known_codes)
