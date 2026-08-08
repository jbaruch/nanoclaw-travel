"""Tests for travel-core's lodging check-in / check-out discriminator.

TripIt writes both sides of a stay as `Lodging` records that differ only in
their summary prefix, so reading the role wrong is silent rather than loud — a
check-out answers `start` just as happily as a check-in does. These pin the
prefix contract three skills now match on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))

from lodging import CHECK_IN, CHECK_OUT, hotel_name, lodging_role  # noqa: E402

HOTEL = "Fairfield Inn & Suites by Marriott Gatlinburg Downtown"


@pytest.mark.parametrize(
    ("summary", "role"),
    [
        (f"Check-in: {HOTEL}", CHECK_IN),
        (f"Check-out: {HOTEL}", CHECK_OUT),
    ],
)
def test_the_prefix_decides_the_role(summary, role):
    assert lodging_role(summary) == role


@pytest.mark.parametrize(
    "summary",
    ["", "Fairfield Inn", "Checkin: Hotel", "check-in: Hotel", None, 42],
    ids=["empty", "no-prefix", "no-hyphen", "lowercased", "none", "not-a-string"],
)
def test_an_unrecognized_summary_has_no_role(summary):
    """Role unknown beats guessing a side — a wrong guess reads a stay as
    starting on its last morning."""
    assert lodging_role(summary) is None


def test_hotel_name_strips_either_prefix():
    assert hotel_name(f"Check-in: {HOTEL}") == HOTEL
    assert hotel_name(f"Check-out: {HOTEL}") == HOTEL


def test_both_sides_of_a_stay_yield_the_same_key():
    """Pairing a stay keys on the name; a prefix left on one side would leave
    the check-out orphaned from its check-in."""
    assert hotel_name(f"Check-in: {HOTEL}") == hotel_name(f"Check-out: {HOTEL}")


@pytest.mark.parametrize("summary", ["Check-in:", "Check-out:   ", "Fairfield Inn", None])
def test_hotel_name_is_none_without_a_usable_name(summary):
    assert hotel_name(summary) is None
