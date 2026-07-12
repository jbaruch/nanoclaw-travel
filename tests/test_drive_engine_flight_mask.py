"""Tests for the identity-based flight mask (#156 R5).

Deterministic fixtures only — hand-written summaries and code sets, no wall-clock
and no time input at all (the mask never consults time). These pin R5: suppress a
meeting-source event only by flight-template summary or a known-designator match,
never by time overlap — so a ground meeting overlapping a redeye flight window
survives.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from flight_mask import (  # noqa: E402
    flight_codes,
    is_flight_event,
    known_flight_codes,
    looks_like_flight_summary,
    normalize_code,
)


def test_normalize_code_strips_space_and_uppercases():
    assert normalize_code("dl 4908") == "DL4908"
    assert normalize_code("FR7382") == "FR7382"


def test_flight_codes_extracts_designators():
    assert flight_codes("Flight to Nashville (DL 4908)") == {"DL4908"}
    assert flight_codes("SK915 / MW 7382 shared") == {"SK915", "MW7382"}
    assert flight_codes("Dinner with Dana") == set()
    assert flight_codes(None) == set()


def test_known_flight_codes_from_raw_codes():
    assert known_flight_codes(["FR7382", "MW 7382", "", None, "SK915"]) == {
        "FR7382",
        "MW7382",
        "SK915",
    }


def test_looks_like_flight_summary():
    assert looks_like_flight_summary("Flight to Copenhagen (SK 915)")
    assert looks_like_flight_summary("✈ CPH → EWR")
    assert not looks_like_flight_summary("Reservation at Fletchers House")
    assert not looks_like_flight_summary(None)


# --- is_flight_event: identity only -----------------------------------------


def test_template_summary_is_masked_even_without_known_codes():
    # Catches a duplicate / TZ-corrupt Gmail flight copy whose time is garbage.
    assert is_flight_event("Flight to Copenhagen (FR 7382)", set())


def test_designator_match_against_known_flight():
    known = known_flight_codes(["FR7382"])
    assert is_flight_event("Some odd label DL0 with FR 7382 in it", known)


def test_codeshare_other_half_matches_when_in_known_set():
    # Canonical flight identity drops the designator, but the mask's known set is
    # built from ALL raw codes, so the MW half of an FR/MW codeshare is recognized.
    known = known_flight_codes(["FR7382", "MW7382"])
    assert is_flight_event("Flight MW 7382", known)


def test_plain_meeting_is_not_masked():
    known = known_flight_codes(["FR7382", "SK915"])
    assert not is_flight_event("Keynote rehearsal with Dana", known)


def test_ground_meeting_overlapping_redeye_survives():
    # R5's explicit case: the mask has no time input, so a real meeting whose time
    # overlaps a flight window is NEVER masked — only identity masks.
    known = known_flight_codes(["SK915"])  # the redeye is SK915
    assert not is_flight_event("Investor breakfast, Copenhagen", known)


def test_unknown_designator_not_masked():
    # A designator that is not a known itinerary flight does not mask — a meeting
    # that merely mentions some other flight code (DL 4908, not in the known set)
    # stays a meeting.
    known = known_flight_codes(["FR7382"])
    assert not is_flight_event("Coordination re DL 4908 pax, room B4", known)
