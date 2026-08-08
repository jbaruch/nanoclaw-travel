"""Tests for travel-core's canonical trip key.

The key joins three artifacts — `travel-db.json`'s trip slugs, the drive
engine's per-trip drive-or-fly verdicts, and the booking-gap snooze store — so
what is pinned here is that every producer computing it from the same trip gets
the same string, whatever shape its start value arrives in.

Fixed summaries and fixed dates only; the function is pure with no clock.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))

from trip_key import trip_key  # noqa: E402


def test_lowercases_dashifies_and_appends_year_month():
    assert trip_key("Madrid Tech Days 2026", "2026-06-15") == "madrid-tech-days-2026-06"


def test_strips_only_a_trailing_year():
    """A 4-digit year mid-summary is part of the name, not the recurrence stamp."""
    assert trip_key("Devoxx 2026 Belgium", "2026-11-02") == "devoxx-2026-belgium-2026-11"


def test_collapses_punctuation_runs_to_single_dashes():
    key = trip_key("TN TIGERS VS Faith Christian School (North Carlolina)", "2026-08-14")
    assert key == "tn-tigers-vs-faith-christian-school-north-carlolina-2026-08"


def test_timed_start_reads_only_the_date_part():
    """Lodging-derived starts carry a time; the key must not vary with it."""
    assert trip_key("Gatlinburg Weekend", "2026-08-14T20:00:00Z") == trip_key(
        "Gatlinburg Weekend", "2026-08-14"
    )


@pytest.mark.parametrize(
    "start",
    ["2026-08-14", date(2026, 8, 14), datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)],
    ids=["iso-string", "date", "datetime"],
)
def test_accepts_every_start_shape_its_callers_hold(start):
    """The DB builder passes the schedule's string; the engine passes a parsed
    date — both must land on one key or the cross-skill read silently misses."""
    assert trip_key("Gatlinburg Weekend", start) == "gatlinburg-weekend-2026-08"


@pytest.mark.parametrize("start", ["", "   ", "not-a-date", None, 20260814])
def test_unparseable_start_raises(start):
    """A bad start fails loudly — a key silently defaulting to today's month
    would write a verdict no reader ever finds."""
    with pytest.raises(ValueError):
        trip_key("Gatlinburg Weekend", start)
