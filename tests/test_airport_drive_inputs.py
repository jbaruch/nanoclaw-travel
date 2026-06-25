"""Tests for the airport drive-block input builder (`airport_drive_inputs.py`).

Deterministic fixtures only — fixed tz-aware datetimes and hand-written byAir
airport payloads, no generated inputs. The module is pure: these pin the window
math (anchor = dep − clearance / arr + post-arrival), the summary text decided in
#90 §10, the tz selection, the domestic/international classification by flag, and
the config-override passthrough.

The builders' output feeds `airport_drive.plan_drive_block` unchanged, so the
last group asserts a built block survives `build_block_args` (the codec the
planner calls) — guarding the seam against drift.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from airport_block import build_block_args, parse_block  # noqa: E402
from airport_drive_inputs import (  # noqa: E402
    AirportContext,
    airport_context,
    arrival_block,
    arrival_summary,
    departure_block,
    departure_summary,
)

CT = timezone(timedelta(hours=-5))  # America/Chicago, summer
DEP = datetime(2026, 7, 2, 14, 0, tzinfo=CT)
ARR = datetime(2026, 7, 2, 16, 30, tzinfo=CT)

US = AirportContext(airport_id=20, flag="🇺🇸", delay_index="low", timezone="America/Chicago")
US_LGA = AirportContext(airport_id=28, flag="🇺🇸", delay_index="low", timezone="America/New_York")
FR = AirportContext(airport_id=99, flag="🇫🇷", delay_index="low", timezone="Europe/Paris")
DE = AirportContext(airport_id=88, flag="🇩🇪", delay_index="low", timezone="Europe/Berlin")


# --- airport_context extraction ------------------------------------------------


def test_airport_context_extracts_the_drive_block_slice():
    payload = {
        "id": 20,
        "code": "BNA",
        "name": "Nashville International",
        "countryName": "United States",
        "countryFlag": "🇺🇸",
        "timezone": "America/Chicago",
        "delay": {"index": "medium", "average": 17},
    }
    ctx = airport_context(payload)
    assert ctx == AirportContext(
        airport_id=20,
        flag="🇺🇸",
        delay_index="medium",
        timezone="America/Chicago",
        code="BNA",
        name="Nashville International",
    )


def test_airport_context_tolerates_missing_and_malformed_fields():
    # No delay object, no flag, no tz — every field degrades to None, no raise.
    assert airport_context({"id": 5}) == AirportContext(airport_id=5)
    # delay present but not a dict, id present but a bool (not a real id).
    assert airport_context({"id": True, "delay": "minor"}) == AirportContext()
    # Entirely non-dict payload.
    assert airport_context(None) == AirportContext()
    assert airport_context("CDG") == AirportContext()


def test_airport_context_drops_empty_strings():
    ctx = airport_context({"countryFlag": "", "timezone": "", "code": "", "delay": {"index": ""}})
    assert ctx == AirportContext()


def test_airport_context_drops_whitespace_only_and_strips_surrounding():
    # A whitespace-only tz is "missing" — it must not survive into the CREATE
    # args and reach the calendar API. Surrounding whitespace is trimmed.
    ctx = airport_context(
        {"countryFlag": "  ", "timezone": "   ", "code": "  BNA  ", "delay": {"index": " "}}
    )
    assert ctx == AirportContext(code="BNA")


# --- summaries (the #90 §10 literals) -----------------------------------------


def test_summaries_match_the_locked_format():
    assert departure_summary("BNA", "DL123") == "Drive: → BNA (DL123)"
    assert arrival_summary("BNA") == "Drive: BNA → home"


# --- departure block (to_airport) ---------------------------------------------


def test_departure_block_domestic_anchors_60_before_dep():
    block = departure_block(
        flight_code="DL123",
        dep_code="BNA",
        dep_ctx=US,
        arr_ctx=US_LGA,
        dep_instant=DEP,
        origin="36.1,-86.6",
        destination="Nashville International",
        baseline_seconds=1800,
    )
    assert block.direction == "to_airport"
    assert block.summary == "Drive: → BNA (DL123)"
    assert block.anchor == DEP - timedelta(minutes=60)  # domestic base clearance
    assert block.leg_start == block.anchor - timedelta(seconds=1800)  # leave-by
    assert block.leg_end is None  # ends at the anchor (the deadline)
    assert block.baseline_seconds == 1800
    assert block.origin == "36.1,-86.6"
    assert block.destination == "Nashville International"
    assert block.timezone == "America/Chicago"  # the DEPARTURE airport's tz
    assert block.kind == "airport_drive_dep"


def test_departure_block_international_anchors_120_before_dep():
    block = departure_block(
        flight_code="DL44",
        dep_code="BNA",
        dep_ctx=US,
        arr_ctx=FR,  # US → FR is international
        dep_instant=DEP,
        origin="home",
        destination="BNA",
        baseline_seconds=1800,
    )
    assert block.anchor == DEP - timedelta(minutes=120)


def test_departure_block_intra_schengen_is_domestic():
    block = departure_block(
        flight_code="AF1",
        dep_code="CDG",
        dep_ctx=FR,
        arr_ctx=DE,  # both Schengen → domestic
        dep_instant=DEP,
        origin="home",
        destination="CDG",
        baseline_seconds=600,
    )
    assert block.anchor == DEP - timedelta(minutes=60)


def test_departure_block_delay_index_nudges_clearance_up():
    busy_dep = AirportContext(flag="🇺🇸", delay_index="high", timezone="America/Chicago")
    block = departure_block(
        flight_code="DL123",
        dep_code="BNA",
        dep_ctx=busy_dep,
        arr_ctx=US_LGA,
        dep_instant=DEP,
        origin="home",
        destination="BNA",
        baseline_seconds=900,
    )
    assert block.anchor == DEP - timedelta(minutes=60 + 30)  # domestic + high nudge


def test_departure_block_undecodable_flag_classifies_international():
    no_flag = AirportContext(timezone="America/Chicago")  # flag None → international
    block = departure_block(
        flight_code="DL123",
        dep_code="BNA",
        dep_ctx=no_flag,
        arr_ctx=US_LGA,
        dep_instant=DEP,
        origin="home",
        destination="BNA",
        baseline_seconds=900,
    )
    assert block.anchor == DEP - timedelta(minutes=120)


def test_departure_block_config_overrides_base_clearance():
    config = {"airport_clearance_domestic_minutes": 45}
    block = departure_block(
        flight_code="DL123",
        dep_code="BNA",
        dep_ctx=US,
        arr_ctx=US_LGA,
        dep_instant=DEP,
        origin="home",
        destination="BNA",
        baseline_seconds=900,
        config=config,
    )
    assert block.anchor == DEP - timedelta(minutes=45)


def test_departure_block_ignores_malformed_config_override():
    # A hand-edited bad value is ignored; the airport_lead default (60) applies.
    for bad in (
        {"airport_clearance_domestic_minutes": "45"},
        {"airport_clearance_domestic_minutes": -5},
        {"airport_clearance_domestic_minutes": True},
    ):
        block = departure_block(
            flight_code="DL123",
            dep_code="BNA",
            dep_ctx=US,
            arr_ctx=US_LGA,
            dep_instant=DEP,
            origin="home",
            destination="BNA",
            baseline_seconds=900,
            config=bad,
        )
        assert block.anchor == DEP - timedelta(minutes=60)


# --- arrival block (from_airport) ---------------------------------------------


def test_arrival_block_domestic_starts_20_after_arr():
    block = arrival_block(
        arr_code="LGA",
        dep_ctx=US,
        arr_ctx=US_LGA,  # domestic arrival
        arr_instant=ARR,
        origin="LGA",
        destination="1 Infinite Loop, Cupertino, CA",
        baseline_seconds=1200,
    )
    assert block.direction == "from_airport"
    assert block.summary == "Drive: LGA → home"
    assert block.anchor == ARR + timedelta(minutes=20)  # domestic post-arrival
    assert block.leg_start == block.anchor  # the drive home STARTS at the anchor
    assert block.leg_end == block.anchor + timedelta(seconds=1200)
    assert block.origin == "LGA"
    assert block.destination == "1 Infinite Loop, Cupertino, CA"
    assert block.timezone == "America/New_York"  # the ARRIVAL airport's tz
    assert block.kind == "airport_drive_arr"


def test_arrival_block_intl_into_us_starts_40_after_arr():
    block = arrival_block(
        arr_code="BNA",
        dep_ctx=FR,  # FR → US is international, arriving into the US
        arr_ctx=US,
        arr_instant=ARR,
        origin="BNA",
        destination="home",
        baseline_seconds=1200,
    )
    assert block.anchor == ARR + timedelta(minutes=40)


def test_arrival_block_intl_abroad_starts_60_after_arr():
    block = arrival_block(
        arr_code="CDG",
        dep_ctx=US,  # US → FR international, arriving abroad
        arr_ctx=FR,
        arr_instant=ARR,
        origin="CDG",
        destination="hotel",
        baseline_seconds=1200,
    )
    assert block.anchor == ARR + timedelta(minutes=60)


def test_arrival_block_config_overrides_post_arrival():
    config = {"airport_post_arrival_intl_us_minutes": 75}
    block = arrival_block(
        arr_code="BNA",
        dep_ctx=FR,
        arr_ctx=US,
        arr_instant=ARR,
        origin="BNA",
        destination="home",
        baseline_seconds=1200,
        config=config,
    )
    assert block.anchor == ARR + timedelta(minutes=75)


# --- validation ----------------------------------------------------------------


def test_naive_instant_rejected():
    naive = datetime(2026, 7, 2, 14, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        departure_block(
            flight_code="DL123",
            dep_code="BNA",
            dep_ctx=US,
            arr_ctx=US_LGA,
            dep_instant=naive,
            origin="home",
            destination="BNA",
            baseline_seconds=900,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        arrival_block(
            arr_code="LGA",
            dep_ctx=US,
            arr_ctx=US_LGA,
            arr_instant=naive,
            origin="LGA",
            destination="home",
            baseline_seconds=900,
        )


def test_bad_baseline_seconds_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        departure_block(
            flight_code="DL123",
            dep_code="BNA",
            dep_ctx=US,
            arr_ctx=US_LGA,
            dep_instant=DEP,
            origin="home",
            destination="BNA",
            baseline_seconds=-1,
        )
    with pytest.raises(ValueError, match="must be an int"):
        arrival_block(
            arr_code="LGA",
            dep_ctx=US,
            arr_ctx=US_LGA,
            arr_instant=ARR,
            origin="LGA",
            destination="home",
            baseline_seconds=12.5,
        )


# --- the seam: built blocks survive the codec the planner calls ----------------


def test_departure_block_round_trips_through_the_block_codec():
    block = departure_block(
        flight_code="DL123",
        dep_code="BNA",
        dep_ctx=US,
        arr_ctx=US_LGA,
        dep_instant=DEP,
        origin="36.1,-86.6",
        destination="BNA",
        baseline_seconds=1800,
    )
    args = build_block_args(
        calendar_id="primary",
        flight_id="12345",
        direction=block.direction,
        summary=block.summary,
        leg_start=block.leg_start,
        anchor=block.anchor,
        baseline_seconds=block.baseline_seconds,
        origin=block.origin,
        destination=block.destination,
        leg_end=block.leg_end,
        timezone=block.timezone,
    )
    assert args["summary"] == "Drive: → BNA (DL123)"
    assert args["timezone"] == "America/Chicago"
    assert args["transparency"] == "transparent"  # Free, #90 decision
    # Parse it back the way the recheck poll would.
    fetched = {"id": "evt_1", "summary": args["summary"], "description": args["description"]}
    state = parse_block(fetched)
    assert state is not None
    assert state.direction == "to_airport"
    assert state.anchor == DEP - timedelta(minutes=60)
    assert state.baseline_seconds == 1800
    assert state.destination == "BNA"


def test_arrival_block_round_trips_through_the_block_codec():
    block = arrival_block(
        arr_code="BNA",
        dep_ctx=FR,
        arr_ctx=US,
        arr_instant=ARR,
        origin="BNA",
        destination="home",
        baseline_seconds=1200,
    )
    args = build_block_args(
        calendar_id="primary",
        flight_id="12345",
        direction=block.direction,
        summary=block.summary,
        leg_start=block.leg_start,
        anchor=block.anchor,
        baseline_seconds=block.baseline_seconds,
        origin=block.origin,
        destination=block.destination,
        leg_end=block.leg_end,
        timezone=block.timezone,
    )
    # Duration spans the drive home: anchor → anchor + 1200s = 20 min.
    assert args["event_duration_hour"] == 0
    assert args["event_duration_minutes"] == 20
    fetched = {"id": "evt_2", "summary": args["summary"], "description": args["description"]}
    state = parse_block(fetched)
    assert state is not None
    assert state.direction == "from_airport"
    assert state.anchor == ARR + timedelta(minutes=40)
    assert state.baseline_leave_by == ARR + timedelta(minutes=40)  # from_airport: anchor itself
