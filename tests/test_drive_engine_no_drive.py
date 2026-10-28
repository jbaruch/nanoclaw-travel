"""Composed no-drive regressions: real scan, routing, reconcile and notice paths."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for bundle in ("travel-core", "flight-assist", "drive-engine"):
    sys.path.insert(0, str(ROOT / "skills" / bundle))

from block_codec import GEN_UNIFIED, ParsedBlock  # noqa: E402
from calendar_apply import apply_plan  # noqa: E402
from itinerary import Itinerary  # noqa: E402
from meeting_source import meeting_desired_blocks  # noqa: E402
from reconcile import plan_reconcile  # noqa: E402
from reconcile_sweep import build_sweep_payload, meeting_anchor  # noqa: E402
from scan import scan  # noqa: E402

NOW = datetime(2020, 7, 13, 9, tzinfo=UTC)
HOME = "12 Example St, TN"
CITY = "Denver, CO"


def _schedule():
    return [
        {
            "type": "Trip",
            "summary": "Denver",
            "start": "2020-07-12",
            "end": "2020-07-15",
            "location": CITY,
        },
        {
            "type": "Flight",
            "summary": "WN1 BNA to DEN",
            "start": "2020-07-12T17:20:00Z",
            "end": "2020-07-12T22:10:00Z",
        },
    ]


def _event(identity="m1", *, hour=12, location="Booth - ExampleConf Denver"):
    return {
        "id": identity,
        "summary": "Meeting",
        "location": location,
        "start": {"dateTime": NOW.replace(hour=hour).isoformat()},
        "end": {"dateTime": NOW.replace(hour=hour + 1).isoformat()},
    }


class Calendar:
    def __init__(self):
        self.created = []

    def create_event(self, args):
        self.created.append(args)
        return {"id": "created"}


def _apply(meetings, route):
    desired, skipped, held = meeting_desired_blocks(meetings, route=route)
    plan = plan_reconcile(desired, [], held=frozenset(h.key for h in held), now=NOW)
    calendar = Calendar()
    applied = apply_plan(plan, calendar=calendar, now=NOW, creation_clock=lambda: NOW)
    return desired, skipped, held, calendar, build_sweep_payload(applied, skipped)


def test_no_lodging_city_is_not_a_meeting_endpoint_or_route():
    itinerary = Itinerary(_schedule(), home_address=HOME, home_airports=frozenset({"BNA"}))
    # The general itinerary city fallback remains useful to its other callers.
    assert itinerary.position_at(NOW).address == CITY
    calls = []

    def route(origin, destination):
        calls.append((origin, destination))
        return timedelta(minutes=20)

    meetings = scan(
        [_event()], now=NOW, home_address=HOME, anchor_for=lambda at: meeting_anchor(itinerary, at)
    )
    desired, _, held, calendar, payload = _apply(meetings, route)
    assert calls == []
    assert desired == []
    assert len(held) == 2
    assert calendar.created == []
    assert payload["data"]["message"] is None


@pytest.mark.parametrize("seconds", [-1, 0, 1, 59, 60])
def test_route_at_or_below_one_minute_has_no_block_or_added_notice(seconds):
    meetings = scan([_event()], now=NOW, home_address=HOME)
    desired, skipped, held, calendar, payload = _apply(
        meetings, lambda _origin, _destination: timedelta(seconds=seconds)
    )
    assert desired == []
    assert held == []  # Proven no-drive, not an uncertain route retaining old blocks.
    assert len(skipped) == 2
    assert calendar.created == []
    assert payload["data"]["message"] is None


def test_real_short_drive_above_floor_still_applies_and_announces():
    meetings = scan([_event()], now=NOW, home_address=HOME)
    desired, _, held, calendar, payload = _apply(
        meetings, lambda _origin, _destination: timedelta(seconds=61)
    )
    assert len(desired) == len(calendar.created) == 2
    assert held == []
    assert "Added a drive" in payload["data"]["message"]


def test_unresolved_trip_anchor_does_not_remove_real_venue_bridge():
    itinerary = Itinerary(_schedule(), home_address=HOME, home_airports=frozenset({"BNA"}))
    first = _event("first", hour=12, location="Venue A")
    first["end"]["dateTime"] = NOW.replace(hour=12, minute=30).isoformat()
    meetings = scan(
        [first, _event("second", hour=13, location="Venue B")],
        now=NOW,
        home_address=HOME,
        anchor_for=lambda at: meeting_anchor(itinerary, at),
    )
    calls = []

    def route(origin, destination):
        calls.append((origin, destination))
        return timedelta(minutes=10)

    desired, _, held, _, _ = _apply(meetings, route)
    assert calls == [("Venue A", "Venue B")]
    assert len(desired) == 1
    assert (desired[0].identity, desired[0].origin, desired[0].destination) == (
        "second",
        "Venue A",
        "Venue B",
    )
    assert len(held) == 2


def test_definitive_zero_route_removes_owned_phantom_without_add_notice():
    meetings = scan([_event()], now=NOW, home_address=HOME)
    desired, skipped, held = meeting_desired_blocks(
        meetings, route=lambda _origin, _destination: timedelta(0)
    )
    current = [
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="phantom",
            identity="m1",
            kind="meeting_outbound",
            baseline_seconds=0,
            anchor=NOW.replace(hour=12),
            origin=HOME,
            destination="Venue",
        )
    ]
    plan = plan_reconcile(desired, current, held=frozenset(h.key for h in held), now=NOW)
    assert len(plan.deletes) == 1
    assert plan.creates == ()
    assert plan.updates == ()
    assert len(skipped) == 2
