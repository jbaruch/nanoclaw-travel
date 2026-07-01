"""Tests for the airport drive block codec (`airport_block.py`).

Deterministic fixtures only — fixed tz-aware datetimes, no generated inputs.
Mirrors the round-trip discipline of test_drive_planner_block_props.py: build
the create args, simulate the fetched event, parse it back, assert equality.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

# E402 suppressed: the sys.path.insert above must execute before this import
# so the skill module resolves by bare name — its bundle dir is only on
# sys.path at runtime, matching nanoclaw-core's import convention.
from airport_block import (  # noqa: E402
    ALERT_GROWTH,
    ALERT_LEAVE_NOW,
    BLOCK_SCHEMA_VERSION,
    BlockState,
    build_block_args,
    build_description,
    build_marker,
    next_alerts,
    parse_block,
    parse_marker,
    serialize_alerted,
)

CT = timezone(timedelta(hours=-5))  # America/Chicago (CDT), fixed offset for tests
DEP = datetime(2026, 7, 2, 14, 0, tzinfo=CT)  # scheduled departure
BE_AT_AIRPORT = DEP - timedelta(minutes=60)  # dep − domestic clearance
LEAVE_BY = BE_AT_AIRPORT - timedelta(minutes=30)  # − 30-min drive


# --- marker ------------------------------------------------------------


def test_marker_round_trip():
    marker = build_marker("12345", "to_airport")
    assert marker == "[flight-assist:flight=12345:dir=to_airport]"
    assert parse_marker(marker) == ("12345", "to_airport")


def test_parse_marker_none_when_absent_or_wrong_type():
    assert parse_marker("no marker here") is None
    assert parse_marker(None) is None
    assert parse_marker(12345) is None


# --- build_description --------------------------------------------------


def test_build_description_shape():
    desc = build_description(
        summary="Drive: → BNA (DL123)",
        flight_id="12345",
        direction="to_airport",
        baseline_seconds=1800,
        anchor=BE_AT_AIRPORT,
        origin="<live location>",
        destination="BNA",
    )
    lines = desc.split("\n")
    assert lines[0] == "Drive: → BNA (DL123)"
    assert lines[1] == "[flight-assist:flight=12345:dir=to_airport]"
    assert lines[2].startswith("<!--fadrive:{") and lines[2].endswith("}-->")


# --- build_block_args ---------------------------------------------------


def test_build_block_args_to_airport_free_with_timezone():
    args = build_block_args(
        calendar_id="primary",
        flight_id="12345",
        direction="to_airport",
        summary="Drive: → BNA (DL123)",
        leg_start=LEAVE_BY,
        anchor=BE_AT_AIRPORT,
        baseline_seconds=1800,
        origin="<live location>",
        destination="BNA",
        timezone="America/Chicago",
    )
    assert args["calendar_id"] == "primary"
    assert args["location"] == "BNA"
    assert args["start_datetime"] == LEAVE_BY.isoformat()
    # 30-minute drive
    assert args["event_duration_hour"] == 0
    assert args["event_duration_minutes"] == 30
    assert args["transparency"] == "transparent"  # Free per #90
    assert args["timezone"] == "America/Chicago"
    assert "[flight-assist:flight=12345:dir=to_airport]" in args["description"]


def test_build_block_args_from_airport_uses_explicit_leg_end():
    arr_anchor = datetime(2026, 7, 2, 18, 40, tzinfo=CT)  # actual_arr + post-arrival
    home_eta = arr_anchor + timedelta(minutes=45)
    args = build_block_args(
        calendar_id="primary",
        flight_id="999",
        direction="from_airport",
        summary="Drive: BNA → home",
        leg_start=arr_anchor,
        anchor=arr_anchor,
        baseline_seconds=45 * 60,
        origin="BNA",
        destination="home",
        leg_end=home_eta,
    )
    assert args["start_datetime"] == arr_anchor.isoformat()
    assert args["event_duration_hour"] == 0
    assert args["event_duration_minutes"] == 45
    assert "timezone" not in args  # omitted when None


def test_build_block_args_busy_is_opaque():
    args = build_block_args(
        calendar_id="primary",
        flight_id="12345",
        direction="to_airport",
        summary="Drive: → BNA",
        leg_start=LEAVE_BY,
        anchor=BE_AT_AIRPORT,
        baseline_seconds=1800,
        origin="o",
        destination="BNA",
        busy=True,
    )
    assert args["transparency"] == "opaque"


def test_build_block_args_validation_errors():
    base = dict(
        calendar_id="primary",
        flight_id="12345",
        direction="to_airport",
        summary="s",
        leg_start=LEAVE_BY,
        anchor=BE_AT_AIRPORT,
        baseline_seconds=1800,
        origin="o",
        destination="BNA",
    )
    naive = datetime(2026, 7, 2, 13, 0)  # no tzinfo
    for bad in (
        {**base, "leg_start": naive},
        {**base, "anchor": naive},
        {**base, "flight_id": ""},
        {**base, "direction": "sideways"},
        {**base, "baseline_seconds": -1},
        {**base, "baseline_seconds": True},  # bool is not a valid int here
        {**base, "origin": ""},
        {**base, "destination": ""},
        {**base, "leg_end": LEAVE_BY - timedelta(minutes=1)},  # end before leg_start
    ):
        try:
            build_block_args(**bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


# --- round trip: build -> simulate fetch -> parse ----------------------


def _fetched_event(args: dict, *, event_id="evt_abc", calendar_id="primary") -> dict:
    """Simulate the Google Calendar event a fetch returns for a created block."""
    return {
        "id": event_id,
        "summary": args["summary"],
        "description": args["description"],
        "calendar_id": calendar_id,
    }


def test_round_trip_to_airport():
    args = build_block_args(
        calendar_id="primary",
        flight_id="12345",
        direction="to_airport",
        summary="Drive: → BNA (DL123)",
        leg_start=LEAVE_BY,
        anchor=BE_AT_AIRPORT,
        baseline_seconds=1800,
        origin="123 Hotel St, Nowhere",
        destination="BNA",
    )
    state = parse_block(_fetched_event(args))
    assert state == BlockState(
        event_id="evt_abc",
        calendar_id="primary",
        flight_id="12345",
        direction="to_airport",
        summary="Drive: → BNA (DL123)",
        baseline_seconds=1800,
        anchor=BE_AT_AIRPORT,
        origin="123 Hotel St, Nowhere",
        destination="BNA",
    )


def test_round_trip_preserves_alerted():
    desc = build_description(
        summary="Drive: → BNA",
        flight_id="12345",
        direction="to_airport",
        baseline_seconds=1800,
        anchor=BE_AT_AIRPORT,
        origin="o",
        destination="BNA",
        alerted={ALERT_GROWTH},
    )
    event = {"id": "e1", "summary": "Drive: → BNA", "description": desc, "calendar_id": "primary"}
    state = parse_block(event)
    assert state is not None
    assert state.already_alerted(ALERT_GROWTH)
    assert not state.already_alerted(ALERT_LEAVE_NOW)


# --- parse_block None paths --------------------------------------------


def test_parse_block_none_when_no_state_comment():
    assert parse_block({"id": "e", "description": "just text, no marker"}) is None


def test_parse_block_none_when_marker_missing():
    # Real state comment present, but the marker line stripped out.
    full = build_description(
        summary="Drive: → BNA",
        flight_id="1",
        direction="to_airport",
        baseline_seconds=1800,
        anchor=BE_AT_AIRPORT,
        origin="o",
        destination="BNA",
    )
    human, _marker, comment = full.split("\n")
    desc = f"{human}\n{comment}"
    assert parse_block({"id": "e", "description": desc}) is None


def _desc(**overrides: Any):
    args: dict[str, Any] = dict(
        summary="Drive: → BNA",
        flight_id="1",
        direction="to_airport",
        baseline_seconds=1800,
        anchor=BE_AT_AIRPORT,
        origin="o",
        destination="BNA",
    )
    args.update(overrides)
    return build_description(**args)


def test_parse_block_none_when_newer_schema_version():
    desc = _desc().replace(
        f'"schema_version":{BLOCK_SCHEMA_VERSION}',
        f'"schema_version":{BLOCK_SCHEMA_VERSION + 1}',
    )
    assert parse_block({"id": "e", "description": desc}) is None


def test_parse_block_none_when_version_not_int():
    desc = _desc().replace(f'"schema_version":{BLOCK_SCHEMA_VERSION}', '"schema_version":"1"')
    assert parse_block({"id": "e", "description": desc}) is None


def test_parse_block_none_when_version_missing():
    # A new artifact has no pre-version legacy records: a record without
    # schema_version is foreign/corrupt and must read as None, never "current".
    desc = _desc().replace(f'"schema_version":{BLOCK_SCHEMA_VERSION},', "")
    assert parse_block({"id": "e", "description": desc}) is None


def test_parse_block_none_when_older_version():
    # Exact-match acceptance: with no migration (v1 is first), an OLDER integer
    # version must not be trusted as current — it reads as no-usable-prior-state.
    desc = _desc().replace(f'"schema_version":{BLOCK_SCHEMA_VERSION}', '"schema_version":0')
    assert parse_block({"id": "e", "description": desc}) is None


def test_parse_block_ignores_calendar_tags_fa_comment():
    # flight-assist's boarding/flight events carry a `<!--fa:{...}-->` tag comment
    # (calendar_tags.py). The airport-block parser must NOT match those — it uses
    # the distinct `<!--fadrive:-->` prefix. Regression for the prefix collision.
    event = {
        "id": "boarding_evt",
        "summary": "Boarding DL123",
        "description": (
            "Boarding DL123\n"
            '<!--fa:{"faKind":"boarding","faFlightId":"12345","faManaged":"created"}-->'
        ),
        "calendar_id": "byair_cal",
    }
    assert parse_block(event) is None


def test_parse_block_none_when_event_id_missing():
    args = build_block_args(
        calendar_id="primary",
        flight_id="1",
        direction="to_airport",
        summary="s",
        leg_start=LEAVE_BY,
        anchor=BE_AT_AIRPORT,
        baseline_seconds=1800,
        origin="o",
        destination="BNA",
    )
    event = {"summary": "s", "description": args["description"]}  # no id
    assert parse_block(event) is None


# --- BlockState computed properties ------------------------------------


def test_baseline_leave_by_to_airport_subtracts_drive():
    state = parse_block(
        _fetched_event(
            build_block_args(
                calendar_id="primary",
                flight_id="1",
                direction="to_airport",
                summary="s",
                leg_start=LEAVE_BY,
                anchor=BE_AT_AIRPORT,
                baseline_seconds=1800,
                origin="o",
                destination="BNA",
            )
        )
    )
    assert state is not None
    # anchor (be-at-airport) − 1800s drive
    assert state.baseline_leave_by == BE_AT_AIRPORT - timedelta(seconds=1800)


def test_baseline_leave_by_from_airport_is_anchor():
    arr_anchor = datetime(2026, 7, 2, 18, 40, tzinfo=CT)
    args = build_block_args(
        calendar_id="primary",
        flight_id="1",
        direction="from_airport",
        summary="Drive: BNA → home",
        leg_start=arr_anchor,
        anchor=arr_anchor,
        baseline_seconds=2700,
        origin="BNA",
        destination="home",
        leg_end=arr_anchor + timedelta(seconds=2700),
    )
    state = parse_block(_fetched_event(args))
    assert state is not None
    assert state.baseline_leave_by == arr_anchor


def test_due_for_recheck_window():
    state = parse_block(
        _fetched_event(
            build_block_args(
                calendar_id="primary",
                flight_id="1",
                direction="to_airport",
                summary="s",
                leg_start=LEAVE_BY,
                anchor=BE_AT_AIRPORT,
                baseline_seconds=1800,
                origin="o",
                destination="BNA",
            )
        )
    )
    assert state is not None
    leave_by = state.baseline_leave_by
    assert state.due_for_recheck(leave_by)  # at leave-by
    assert state.due_for_recheck(leave_by - timedelta(minutes=44))  # inside horizon
    assert not state.due_for_recheck(leave_by - timedelta(minutes=46))  # before horizon
    assert not state.due_for_recheck(leave_by + timedelta(minutes=16))  # past grace


# --- next_alerts --------------------------------------------------------


def test_next_alerts_fires_once_each():
    fire, alerted = next_alerts(frozenset(), grew=True, leave_now=False)
    assert fire == (ALERT_GROWTH,)
    assert alerted == frozenset({ALERT_GROWTH})

    fire, alerted = next_alerts(alerted, grew=True, leave_now=True)
    assert fire == (ALERT_LEAVE_NOW,)  # growth already fired; only leave_now is new
    assert alerted == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})

    fire, alerted = next_alerts(alerted, grew=True, leave_now=True)
    assert fire == ()  # both already fired -> silent


def test_serialize_alerted_stable_order():
    assert serialize_alerted({ALERT_LEAVE_NOW, ALERT_GROWTH}) == "growth,leave_now"
    assert serialize_alerted(frozenset()) == ""
