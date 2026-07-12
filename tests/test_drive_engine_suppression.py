"""Tests for trivial-leg suppression (#156 R6 / V3 / G6).

Deterministic fixtures only — fixed timedeltas, no wall-clock. These pin the
conditional suppression: a routed drive at/under the trivial threshold is
suppressed ONLY when a boarding / time-to-leave presence block exists; a trivial
drive with no presence block is never silently suppressed.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from suppression import TRIVIAL_LEG_THRESHOLD, is_trivial_leg  # noqa: E402


def test_trivial_with_presence_block_is_suppressed():
    assert is_trivial_leg(timedelta(minutes=8), presence_block_present=True)


def test_at_threshold_is_trivial():
    assert is_trivial_leg(TRIVIAL_LEG_THRESHOLD, presence_block_present=True)


def test_just_over_threshold_is_not_trivial():
    assert not is_trivial_leg(
        TRIVIAL_LEG_THRESHOLD + timedelta(seconds=1), presence_block_present=True
    )


def test_trivial_without_presence_block_is_not_suppressed():
    # R6: never suppress silently — the presence block is the only "head to the
    # gate" signal, so absent it the trivial drive block stays.
    assert not is_trivial_leg(timedelta(minutes=5), presence_block_present=False)


def test_long_drive_never_trivial_even_with_presence_block():
    assert not is_trivial_leg(timedelta(minutes=45), presence_block_present=True)


def test_custom_threshold():
    assert is_trivial_leg(
        timedelta(minutes=14), presence_block_present=True, threshold=timedelta(minutes=15)
    )


def test_negative_drive_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        is_trivial_leg(timedelta(seconds=-1), presence_block_present=True)
