"""Tests for the drive-engine scan classifier (`scan.py`).

Every test maps to a concrete behavior the scan must get right; the
neighbour / idempotency / skip / past tests are named after the LoMBot
`drive_planner` issues whose scars they encode (Epic #59 §5). Fixtures are
built programmatically with the real Google Calendar event *structure*
(timed `dateTime` blocks, `location`, marker-bearing `description`) but
synthetic ids and venues — no live calendar, no real user data.
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from scan import (  # noqa: E402
    DEFAULT_TIGHT_GAP_SECONDS,
    MeetingClass,
    ScanError,
    TransitLeg,
    actionable,
    flight_codes,
    same_place,
    scan,
)

# Fixed central-time-ish offset so fixtures are deterministic without a
# tzdata dependency; the scan only needs tz-aware datetimes, not a named zone.
CT = timezone(timedelta(hours=-5))
NOW = datetime(2026, 7, 1, 8, 0, tzinfo=CT)
HOME = "12 Example St, Sampleton, TN 37000"


def _timed(start: datetime, end: datetime) -> dict:
    return {
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Chicago"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Chicago"},
    }


def _meeting(
    event_id: str,
    *,
    start: datetime,
    end: datetime,
    location: str | None = "100 Broadway, Nashville, TN",
    summary: str = "Customer sync",
    description: str = "",
    attendees: list | None = None,
    status: str | None = None,
) -> dict:
    event: dict = {"id": event_id, "summary": summary, "description": description}
    event.update(_timed(start, end))
    if location is not None:
        event["location"] = location
    if attendees is not None:
        event["attendees"] = attendees
    if status is not None:
        event["status"] = status
    return event


def _self_rsvp(response_status: str) -> list:
    """An attendees list where the operator's own row carries `response_status`."""
    return [
        {"email": "someone@else.com", "responseStatus": "accepted"},
        {"email": "me@me.com", "self": True, "responseStatus": response_status},
    ]


def _block(served_id: str, direction: str, *, event_id: str | None = None) -> dict:
    """A planner-created block carrying the self-recognition marker."""
    return _meeting(
        event_id or f"block_{served_id}_{direction}",
        start=NOW + timedelta(hours=1),
        end=NOW + timedelta(hours=2),
        location=HOME,
        summary="\U0001f697 Drive",
        description=f"[drive-planner:meeting={served_id}:dir={direction}]",
    )


def _by_id(results: list[MeetingClass]) -> dict[str, MeetingClass]:
    return {r.meeting_id: r for r in results}


# --- baseline -------------------------------------------------------------


def test_standalone_meeting_is_needs_decision_with_both_legs():
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=1)
    [result] = scan([_meeting("m1", start=start, end=end)], now=NOW, home_address=HOME)

    assert result.bucket == "needs_decision"
    directions = [leg.direction for leg in result.legs]
    assert directions == ["outbound", "return"]
    outbound, ret = result.legs
    assert outbound.origin == HOME
    assert outbound.arrive_by == start
    assert ret.destination == HOME
    assert ret.depart_after == end


def test_nothing_is_silently_dropped():
    events = [
        _meeting("m1", start=NOW + timedelta(hours=3), end=NOW + timedelta(hours=4)),
        _meeting("allday", start=NOW, end=NOW, location="X"),
        _block("m1", "outbound"),
    ]
    events[1].pop("start")
    events[1]["start"] = {"date": "2026-07-01"}
    events[1]["end"] = {"date": "2026-07-02"}
    results = scan(events, now=NOW, home_address=HOME)
    assert len(results) == len(events)


# --- lombot #50: ANY marker = handled, idempotent -------------------------


def test_lombot50_any_marker_makes_meeting_has_block():
    start = NOW + timedelta(hours=3)
    events = [
        _meeting("m1", start=start, end=start + timedelta(hours=1)),
        _block("m1", "outbound"),  # outbound ONLY — not both directions
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert by_id["m1"].bucket == "has_block"
    assert by_id["m1"].present_directions == ("outbound",)
    assert by_id["m1"].legs == ()


def test_lombot50_present_directions_dedup_both_legs():
    start = NOW + timedelta(hours=3)
    events = [
        _meeting("m1", start=start, end=start + timedelta(hours=1)),
        _block("m1", "outbound", event_id="b1"),
        _block("m1", "return", event_id="b2"),
        _block("m1", "outbound", event_id="b3"),  # duplicate direction
    ]
    result = _by_id(scan(events, now=NOW, home_address=HOME))["m1"]
    assert result.bucket == "has_block"
    assert set(result.present_directions) == {"outbound", "return"}
    assert len(result.present_directions) == 2  # deduped


def test_planner_block_itself_is_filtered():
    result = scan([_block("m1", "outbound")], now=NOW, home_address=HOME)[0]
    assert result.bucket == "filtered"
    assert result.reason == "planner block"


# --- lombot #49: skips persist with expiry; virtual never asked ----------


def test_lombot49_active_skip_is_skipped():
    start = NOW + timedelta(hours=3)
    skip = {"m1": (NOW + timedelta(days=2)).isoformat()}
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
        skip_state=skip,
    )[0]
    assert result.bucket == "skipped"


def test_lombot49_expired_skip_reverts_to_needs_decision():
    start = NOW + timedelta(hours=3)
    skip = {"m1": (NOW - timedelta(days=1)).isoformat()}
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
        skip_state=skip,
    )[0]
    assert result.bucket == "needs_decision"


def test_lombot49_malformed_skip_expiry_reverts_to_needs_decision():
    start = NOW + timedelta(hours=3)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
        skip_state={"m1": "not-a-date"},
    )[0]
    assert result.bucket == "needs_decision"


@pytest.mark.parametrize(
    "location",
    [
        "https://zoom.us/j/123",
        "meet.google.com/abc-defg-hij",
        "Microsoft Teams Meeting (teams.microsoft.com/l/x)",
        "Online",
        "Phone call",
    ],
)
def test_lombot49_virtual_locations_are_filtered(location):
    start = NOW + timedelta(hours=3)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1), location=location)],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "filtered"
    assert result.reason == "virtual location"


def test_missing_location_is_filtered():
    start = NOW + timedelta(hours=3)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1), location=None)],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "filtered"
    assert result.reason == "no location"


def test_all_day_event_is_filtered():
    event = {
        "id": "m1",
        "summary": "Conference",
        "location": "Austin, TX",
        "start": {"date": "2026-07-02"},
        "end": {"date": "2026-07-03"},
    }
    result = scan([event], now=NOW, home_address=HOME)[0]
    assert result.bucket == "filtered"
    assert result.reason == "all-day event"


def test_declined_meeting_is_filtered():
    # The operator declined it — never plan a drive there.
    event = _meeting(
        "m1",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=3),
        attendees=_self_rsvp("declined"),
    )
    result = scan([event], now=NOW, home_address=HOME)[0]
    assert result.bucket == "filtered"
    assert result.reason == "operator declined the meeting"


@pytest.mark.parametrize("rsvp", ["accepted", "tentative", "needsAction"])
def test_non_declined_rsvp_still_plans(rsvp):
    # Accepted / tentative / no-response all still get a drive block.
    event = _meeting(
        "m1",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=3),
        attendees=_self_rsvp(rsvp),
    )
    result = scan([event], now=NOW, home_address=HOME)[0]
    assert result.bucket == "needs_decision"


def test_cancelled_event_is_filtered():
    event = _meeting(
        "m1", start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3), status="cancelled"
    )
    result = scan([event], now=NOW, home_address=HOME)[0]
    assert result.bucket == "filtered"
    assert result.reason == "event cancelled"


def test_meeting_timezone_is_captured():
    # The meeting's start.timeZone flows onto the MeetingClass so the block
    # CREATE can pass an explicit IANA timezone (#83 — else it lands hours off).
    [result] = scan(
        [_meeting("m1", start=NOW + timedelta(hours=3), end=NOW + timedelta(hours=4))],
        now=NOW,
        home_address=HOME,
    )
    assert result.timezone == "America/Chicago"


def test_meeting_timezone_falls_back_to_etc_offset_when_no_iana():
    # A block missing its IANA timeZone but carrying an offset still anchors via
    # a fixed-offset Etc/GMT zone (-05:00 -> Etc/GMT+5).
    event = _meeting("m1", start=NOW + timedelta(hours=3), end=NOW + timedelta(hours=4))
    del event["start"]["timeZone"]
    [result] = scan([event], now=NOW, home_address=HOME)
    assert result.timezone == "Etc/GMT+5"


def test_declined_neighbour_does_not_make_meeting_back_to_back():
    # A declined same-venue meeting must not strip a real meeting's home legs.
    venue = "100 Broadway, Nashville, TN"
    declined = _meeting(
        "d",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=2, minutes=30),
        location=venue,
        attendees=_self_rsvp("declined"),
    )
    real = _meeting(
        "r",
        start=NOW + timedelta(hours=3),
        end=NOW + timedelta(hours=4),
        location=venue,
    )
    results = _by_id(scan([declined, real], now=NOW, home_address=HOME))
    assert results["d"].bucket == "filtered"
    # `r` keeps its outbound-from-home + return legs (not back_to_back).
    assert results["r"].bucket == "needs_decision"
    assert sorted(leg.direction for leg in results["r"].legs) == ["outbound", "return"]


# --- lombot #28: past guard ----------------------------------------------


def test_lombot28_past_meeting_is_bucketed_past():
    start = NOW - timedelta(hours=2)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "past"
    assert result.legs == ()


def test_lombot28_just_started_within_tolerance_is_not_past():
    start = NOW - timedelta(minutes=2)  # inside PAST_TOLERANCE
    result = scan(
        [_meeting("m1", start=start, end=NOW + timedelta(minutes=58))],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "needs_decision"


def test_lombot28_past_neighbour_does_not_suppress_future_outbound():
    # The OpenAI reviewer's repro for PR #73: a past same-venue meeting must
    # not pull a future meeting into back_to_back and strip its outbound leg.
    venue = "100 Broadway, Nashville, TN"
    past_start = NOW - timedelta(minutes=135)
    past_end = NOW - timedelta(minutes=75)  # ends 75 min before now
    future_start = NOW + timedelta(minutes=10)  # 85-min gap → "tight", same venue
    future_end = future_start + timedelta(hours=1)
    events = [
        _meeting("past", start=past_start, end=past_end, location=venue),
        _meeting("future", start=future_start, end=future_end, location=venue),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert by_id["past"].bucket == "past"
    # The future meeting is standalone — the past neighbour is NOT linked.
    assert by_id["future"].bucket == "needs_decision"
    assert [leg.direction for leg in by_id["future"].legs] == ["outbound", "return"]


# --- #85: flight events filtered by three signals, never ground-routed -----


def test_flight_event_overlapping_window_is_filtered():
    # Signal 1: a flight event whose span overlaps a known flight window. Give
    # it a non-flight-template summary so this isolates the time-overlap signal.
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=2)
    flight = _meeting(
        "flt1",
        start=start,
        end=end,
        location="John F. Kennedy International Airport (JFK), Queens, NY 11430, USA",
        summary="DL4908 segment",  # no "Flight to"/✈ prefix, no scheduled code
    )
    result = scan([flight], now=NOW, home_address=HOME, flight_windows=[(start, end)])[0]
    assert result.bucket == "filtered"
    assert result.reason == "air travel — flight event"
    assert result.legs == ()


def test_flight_window_partial_overlap_still_filters():
    # Any interval overlap marks it a flight; again a non-template summary so
    # only the window signal can fire.
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=2)
    window = (start + timedelta(minutes=30), end + timedelta(minutes=30))
    result = scan(
        [_meeting("flt1", start=start, end=end, summary="airport transfer")],
        now=NOW,
        home_address=HOME,
        flight_windows=[window],
    )[0]
    assert result.bucket == "filtered"
    assert result.reason == "air travel — flight event"


def test_flight_template_summary_filtered_without_any_schedule():
    # Signal 2 (intrinsic): a "Flight to …" summary is air travel even with NO
    # flight windows or summaries — this is what catches the duplicate Gmail
    # flight events whose corrupted timezone misses the window.
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=2)
    for summary in ("Flight to Nashville (DL 4908)", "✈ BNA→YYZ • UA 8018"):
        result = scan(
            [_meeting("flt", start=start, end=end, location="New York, NY, USA", summary=summary)],
            now=NOW,
            home_address=HOME,
        )[0]
        assert result.bucket == "filtered", summary
        assert result.reason == "air travel — flight event"


def test_corrupted_duplicate_flight_still_filtered_by_summary():
    # The exact recurrence: three "Flight to Nashville (DL 4908)" Gmail copies,
    # two with a corrupted timezone whose span (19:55–22:01Z) ends before the
    # true flight window (22:59–01:46Z) starts. Signal 1 misses the corrupt
    # copies; the template summary (signal 2) catches all three.
    good_start = datetime(2026, 7, 1, 22, 59, tzinfo=timezone.utc)
    good_end = datetime(2026, 7, 2, 1, 46, tzinfo=timezone.utc)
    bad_start = datetime(2026, 7, 1, 19, 55, tzinfo=timezone.utc)
    bad_end = datetime(2026, 7, 1, 22, 1, tzinfo=timezone.utc)
    events = [
        _meeting(
            "good",
            start=good_start,
            end=good_end,
            location="New York, NY, USA",
            summary="Flight to Nashville (DL 4908)",
        ),
        _meeting(
            "bad1",
            start=bad_start,
            end=bad_end,
            location="New York, NY, USA",
            summary="Flight to Nashville (DL 4908)",
        ),
        _meeting(
            "bad2",
            start=bad_start,
            end=bad_end,
            location="New York, NY, USA",
            summary="Flight to Nashville (DL 4908)",
        ),
    ]
    results = scan(
        events,
        now=good_start - timedelta(hours=2),
        home_address=HOME,
        flight_windows=[(good_start, good_end)],
    )
    assert {r.bucket for r in results} == {"filtered"}


def test_flight_code_matches_schedule_when_time_and_template_miss():
    # Signal 3: a flight event with a non-template summary and a corrupted time
    # that misses the window is still caught when its IATA code matches a
    # scheduled flight's summary.
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=2)
    far_window = (NOW + timedelta(days=1), NOW + timedelta(days=1, hours=2))
    flight = _meeting(
        "flt", start=start, end=end, location="New York, NY, USA", summary="DL 4908 NYC"
    )
    result = scan(
        [flight],
        now=NOW,
        home_address=HOME,
        flight_windows=[far_window],
        flight_summaries=["DL 4908 London Stansted to New York JFK"],
    )[0]
    assert result.bucket == "filtered"
    assert result.reason == "air travel — flight event"


def test_gmail_reservation_is_not_filtered_as_flight():
    # A Gmail-auto-created restaurant reservation is a legitimate drive target —
    # it must NOT be filtered. Only flight-shaped events are air travel.
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=2)
    result = scan(
        [
            _meeting(
                "res",
                start=start,
                end=end,
                location="Fletchers House, Rye, UK",
                summary="Reservation at Fletchers House",
            )
        ],
        now=NOW,
        home_address=HOME,
        flight_summaries=["DL 4908 London to New York"],
    )[0]
    assert result.bucket == "needs_decision"
    assert [leg.direction for leg in result.legs] == ["outbound", "return"]


def test_meeting_outside_flight_windows_is_not_suppressed():
    # A real meeting that overlaps no window, has no flight template, and no
    # matching code plans normally — the filter never suppresses a real meeting.
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=1)
    far_window = (NOW + timedelta(days=1), NOW + timedelta(days=1, hours=2))
    result = scan(
        [_meeting("m1", start=start, end=end)],
        now=NOW,
        home_address=HOME,
        flight_windows=[far_window],
        flight_summaries=["DL 4908 to New York"],
    )[0]
    assert result.bucket == "needs_decision"
    assert [leg.direction for leg in result.legs] == ["outbound", "return"]


def test_flight_neighbour_does_not_bridge_real_meeting():
    # The #85 symptom: a flight event tight against a real meeting must not act
    # as a bridge neighbour the real meeting drives to/from across an ocean.
    flight_start = NOW + timedelta(hours=3)
    flight_end = flight_start + timedelta(hours=2)
    meeting_start = flight_end + timedelta(minutes=30)  # tight gap to the flight
    meeting_end = meeting_start + timedelta(hours=1)
    events = [
        _meeting(
            "flt",
            start=flight_start,
            end=flight_end,
            location="John F. Kennedy International Airport (JFK), Queens, NY 11430, USA",
            summary="Flight to Nashville (DL 4908)",
        ),
        _meeting(
            "mtg", start=meeting_start, end=meeting_end, location="100 Broadway, Nashville, TN"
        ),
    ]
    by_id = _by_id(
        scan(events, now=NOW, home_address=HOME, flight_windows=[(flight_start, flight_end)])
    )
    assert by_id["flt"].bucket == "filtered"
    # The real meeting is standalone — it never bridges from the flight.
    assert by_id["mtg"].bucket == "needs_decision"
    assert [leg.direction for leg in by_id["mtg"].legs] == ["outbound", "return"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Flight to Nashville (DL 4908)", {"DL4908"}),
        ("DL 4908 London to New York", {"DL4908"}),
        ("dl4908 lower", {"DL4908"}),  # case-insensitive, no space
        ("U2 123 and 9W 456", {"U2123", "9W456"}),  # letter-digit / digit-letter codes
        ("Customer sync at 3pm", set()),  # no designator
        ("Reservation at Fletchers House", set()),  # a real ground meeting
        ("", set()),
        (None, set()),
    ],
)
def test_flight_codes_extraction(text, expected):
    assert set(flight_codes(text)) == expected


def test_non_flight_meeting_without_schedule_still_plans():
    # Without any flight context, an ordinary meeting (no flight template, no
    # window, no code) plans normally — the intrinsic summary signal is narrow.
    start = NOW + timedelta(hours=3)
    end = start + timedelta(hours=2)
    result = scan(
        [_meeting("m1", start=start, end=end, summary="Customer sync")],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.bucket == "needs_decision"


# --- lombot #37: multiline location normalized ---------------------------


def test_z_suffix_datetime_is_parsed_not_filtered():
    # RFC3339 UTC `Z` must parse (some sources emit it), not fall through to
    # "unparseable" and silently drop the meeting.
    event = {
        "id": "m1",
        "summary": "Customer sync",
        "location": "100 Broadway, Nashville, TN",
        "start": {"dateTime": "2026-07-01T14:00:00Z"},
        "end": {"dateTime": "2026-07-01T15:00:00Z"},
    }
    result = scan([event], now=NOW, home_address=HOME)[0]
    assert result.bucket == "needs_decision"
    assert result.start is not None and result.start.tzinfo is not None


def test_naive_datetime_event_is_filtered_not_crash():
    # A timezone-naive dateTime can't be compared to the tz-aware `now`;
    # it must be filtered as unparseable, never raise TypeError.
    event = {
        "id": "m1",
        "summary": "Customer sync",
        "location": "100 Broadway, Nashville, TN",
        "start": {"dateTime": "2026-07-01T14:00:00"},  # no offset
        "end": {"dateTime": "2026-07-01T15:00:00"},
    }
    result = scan([event], now=NOW, home_address=HOME)[0]
    assert result.bucket == "filtered"
    assert result.reason == "missing or unparseable time"


def test_naive_skip_expiry_is_ignored_not_crash():
    start = NOW + timedelta(hours=3)
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1))],
        now=NOW,
        home_address=HOME,
        skip_state={"m1": "2026-07-03T00:00:00"},  # naive → unusable → re-ask
    )[0]
    assert result.bucket == "needs_decision"


def test_non_dict_time_block_is_filtered_not_crash():
    event = {
        "id": "m1",
        "summary": "Customer sync",
        "location": "100 Broadway, Nashville, TN",
        "start": "2026-07-01T14:00:00-05:00",  # a string, not a {dateTime} block
        "end": "2026-07-01T15:00:00-05:00",
    }
    result = scan([event], now=NOW, home_address=HOME)[0]
    assert result.bucket == "filtered"


def test_lombot37_multiline_location_is_whitespace_normalized():
    start = NOW + timedelta(hours=3)
    messy = "Acme HQ\n  500 Main St\tSuite 4\nNashville,  TN"
    result = scan(
        [_meeting("m1", start=start, end=start + timedelta(hours=1), location=messy)],
        now=NOW,
        home_address=HOME,
    )[0]
    assert result.location == "Acme HQ 500 Main St Suite 4 Nashville, TN"


# --- lombot #14/#7: neighbour-aware (same vs different venue, tight gap) --


def test_lombot14_same_venue_tight_gap_is_back_to_back():
    venue = "100 Broadway, Nashville, TN"
    a_start = NOW + timedelta(hours=3)
    a_end = a_start + timedelta(hours=1)
    b_start = a_end + timedelta(minutes=15)  # tight
    b_end = b_start + timedelta(hours=1)
    events = [
        _meeting("a", start=a_start, end=a_end, location=venue),
        _meeting("b", start=b_start, end=b_end, location=venue),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    # First of the same-venue run: outbound from home, NO return (you stay).
    assert [leg.direction for leg in by_id["a"].legs] == ["outbound"]
    # Second: no inbound leg (already there), return home. It is back_to_back.
    assert by_id["b"].bucket == "back_to_back"
    assert [leg.direction for leg in by_id["b"].legs] == ["return"]


def test_lombot7_different_venue_tight_gap_is_bridge_with_gap_exposed():
    a_start = NOW + timedelta(hours=3)
    a_end = a_start + timedelta(hours=1)
    b_start = a_end + timedelta(minutes=30)  # tight, different venue
    b_end = b_start + timedelta(hours=1)
    events = [
        _meeting("a", start=a_start, end=a_end, location="100 Broadway, Nashville"),
        _meeting("b", start=b_start, end=b_end, location="900 Division St, Nashville"),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert by_id["b"].bucket == "bridge"
    bridge_legs = [leg for leg in by_id["b"].legs if leg.direction == "bridge"]
    assert len(bridge_legs) == 1
    bridge = bridge_legs[0]
    assert bridge.origin == "100 Broadway, Nashville"
    assert bridge.destination == "900 Division St, Nashville"
    # gap exposed so the router can warn when drive_time > gap (lombot #14/#7)
    assert bridge.gap_seconds == 30 * 60
    assert bridge.arrive_by == b_start


def test_large_gap_same_day_keeps_both_independent():
    a_start = NOW + timedelta(hours=3)
    a_end = a_start + timedelta(hours=1)
    b_start = a_end + timedelta(hours=4)  # well over the tight threshold
    b_end = b_start + timedelta(hours=1)
    events = [
        _meeting("a", start=a_start, end=a_end, location="100 Broadway"),
        _meeting("b", start=b_start, end=b_end, location="900 Division St"),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert by_id["a"].bucket == "needs_decision"
    assert [leg.direction for leg in by_id["a"].legs] == ["outbound", "return"]
    assert by_id["b"].bucket == "needs_decision"
    assert [leg.direction for leg in by_id["b"].legs] == ["outbound", "return"]


def test_three_same_venue_run_anchors_outbound_first_return_last():
    venue = "100 Broadway, Nashville, TN"
    starts = [NOW + timedelta(hours=3, minutes=90 * i) for i in range(3)]
    events = [
        _meeting(f"m{i}", start=s, end=s + timedelta(minutes=30), location=venue)
        for i, s in enumerate(starts)
    ]
    # 30-min meetings, 60-min gaps → tight (≤ 90 min) and same venue.
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))

    assert [leg.direction for leg in by_id["m0"].legs] == ["outbound"]
    assert by_id["m1"].bucket == "back_to_back"
    assert by_id["m1"].legs == ()  # middle of the run: no transit at all
    assert [leg.direction for leg in by_id["m2"].legs] == ["return"]


def test_tight_gap_threshold_is_configurable():
    a_start = NOW + timedelta(hours=3)
    a_end = a_start + timedelta(hours=1)
    b_start = a_end + timedelta(minutes=40)
    b_end = b_start + timedelta(hours=1)
    events = [
        _meeting("a", start=a_start, end=a_end, location="100 Broadway"),
        _meeting("b", start=b_start, end=b_end, location="900 Division St"),
    ]
    # With a 30-min threshold, a 40-min gap is NOT tight → independent trips.
    by_id = _by_id(scan(events, now=NOW, home_address=HOME, tight_gap_seconds=30 * 60))
    assert by_id["b"].bucket == "needs_decision"


# --- helpers and input guards --------------------------------------------


def test_actionable_filters_to_action_buckets():
    start = NOW + timedelta(hours=3)
    events = [
        _meeting("decide", start=start, end=start + timedelta(hours=1)),
        _block("decide2", "outbound"),
    ]
    events.append(_meeting("decide2", start=start, end=start + timedelta(hours=1)))
    results = scan(events, now=NOW, home_address=HOME)
    act = actionable(results)
    assert {r.meeting_id for r in act} == {"decide"}


def test_naive_now_raises_scan_error():
    with pytest.raises(ScanError, match="timezone-naive"):
        scan([], now=datetime(2026, 7, 1, 8, 0), home_address=HOME)


def test_empty_home_address_raises_scan_error():
    with pytest.raises(ScanError, match="home_address"):
        scan([], now=NOW, home_address="")


def test_events_must_be_a_list():
    with pytest.raises(ScanError, match="must be a list"):
        scan({"id": "m1"}, now=NOW, home_address=HOME)  # type: ignore[arg-type]


def test_non_dict_skip_state_raises_scan_error():
    with pytest.raises(ScanError, match="skip_state"):
        scan([], now=NOW, home_address=HOME, skip_state=["evt_1"])  # type: ignore[arg-type]


def test_malformed_event_elements_are_filtered_not_crash():
    # A non-dict element, and dict events with non-string location/description,
    # must classify (filtered) without raising, and must NOT abort the good
    # event that follows them in the same batch.
    start = NOW + timedelta(hours=3)
    good = _meeting("good", start=start, end=start + timedelta(hours=1))
    events = [
        "not-an-event",  # non-dict element
        123,  # non-dict element
        {"id": "m1", "summary": "x", "location": ["a", "b"], **_timed(start, start)},
        {"id": "m2", "summary": "x", "description": {"nested": 1}, **_timed(start, start)},
        good,
    ]
    results = scan(events, now=NOW, home_address=HOME)  # type: ignore[arg-type]
    assert len(results) == len(events)  # nothing dropped
    assert results[0].bucket == "filtered"
    assert results[1].bucket == "filtered"
    # the good event still classifies normally
    assert results[-1].meeting_id == "good"
    assert results[-1].bucket == "needs_decision"


def test_default_threshold_constant_is_ninety_minutes():
    assert DEFAULT_TIGHT_GAP_SECONDS == 90 * 60


def test_transit_leg_is_frozen():
    leg = TransitLeg(direction="outbound", origin=HOME, destination="X")
    with pytest.raises(FrozenInstanceError):
        leg.direction = "return"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Per-meeting anchor resolution (#122)
# ---------------------------------------------------------------------------

LODGING = "1 Seaside Lane, Hastings, UK"


def test_anchor_for_replaces_home_on_outbound_and_return():
    """On a trip, the home-side endpoints of both legs are the resolved
    anchor (the current lodging) — never the static home (#122)."""
    event = _meeting(
        "evt_trip",
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=1),
        location="Rye Waterworks, Rye, UK",
    )
    results = scan(
        [event],
        now=NOW,
        home_address=HOME,
        anchor_for=lambda _at: (LODGING, None),
    )
    (outbound, ret) = results[0].legs
    assert outbound.origin == LODGING
    assert outbound.destination == "Rye Waterworks, Rye, UK"
    assert ret.origin == "Rye Waterworks, Rye, UK"
    assert ret.destination == LODGING
    assert outbound.anchor_note is None


def test_anchor_for_is_resolved_per_meeting_start():
    """A 14-day sweep window spans on-trip and off-trip meetings — each
    meeting anchors at its OWN start time, not one global answer."""
    on_trip_day = NOW + timedelta(days=1)
    home_day = NOW + timedelta(days=10)
    events = [
        _meeting("evt_uk", start=on_trip_day, end=on_trip_day + timedelta(hours=1)),
        _meeting("evt_home", start=home_day, end=home_day + timedelta(hours=1)),
    ]

    def anchor_for(at):
        return (LODGING, None) if at < NOW + timedelta(days=5) else (HOME, None)

    results = scan(events, now=NOW, home_address=HOME, anchor_for=anchor_for)
    by_id = {r.meeting_id: r for r in results}
    assert by_id["evt_uk"].legs[0].origin == LODGING
    assert by_id["evt_home"].legs[0].origin == HOME


def test_unresolved_anchor_emits_none_endpoints_with_note():
    """On a trip before any lodging is known: the legs carry None
    home-side endpoints plus the resolver's note — surfaced, never routed
    from home."""
    event = _meeting(
        "evt_early",
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=1),
    )
    note = "on 'UK trip' with no lodging event at or before the meeting"
    results = scan(
        [event],
        now=NOW,
        home_address=HOME,
        anchor_for=lambda _at: (None, note),
    )
    (outbound, ret) = results[0].legs
    assert results[0].bucket == "needs_decision"
    assert outbound.origin is None
    assert outbound.anchor_note == note
    assert ret.destination is None
    assert ret.anchor_note == note


def test_bridge_leg_is_venue_to_venue_regardless_of_anchor():
    """A bridge never touches the anchor — venue→venue survives even an
    unresolved anchor."""
    first = _meeting(
        "evt_first",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=3),
        location="Venue A, Rye, UK",
    )
    second = _meeting(
        "evt_second",
        start=NOW + timedelta(hours=3, minutes=15),
        end=NOW + timedelta(hours=4),
        location="Venue B, Hastings, UK",
    )
    results = scan(
        [first, second],
        now=NOW,
        home_address=HOME,
        anchor_for=lambda _at: (None, "no anchor"),
    )
    by_id = {r.meeting_id: r for r in results}
    bridge = by_id["evt_second"].legs[0]
    assert bridge.direction == "bridge"
    assert bridge.origin == "Venue A, Rye, UK"
    assert bridge.destination == "Venue B, Hastings, UK"


def test_default_anchor_is_home():
    """Without anchor_for the scan behaves exactly as before #122."""
    event = _meeting(
        "evt_plain",
        start=NOW + timedelta(days=1),
        end=NOW + timedelta(days=1, hours=1),
    )
    results = scan([event], now=NOW, home_address=HOME)
    assert results[0].legs[0].origin == HOME
    assert results[0].legs[-1].destination == HOME


# ---------------------------------------------------------------------------
# A meeting AT the anchor — held at home, or at the trip lodging (#301)
# ---------------------------------------------------------------------------


def test_meeting_at_home_is_filtered_with_no_legs():
    """The #301 symptom: an appointment whose location IS `current_home` drew
    two degenerate home→home drive blocks. There is nowhere to drive."""
    start = NOW + timedelta(hours=3)
    event = _meeting("evt_home", start=start, end=start + timedelta(hours=1), location=HOME)
    [result] = scan([event], now=NOW, home_address=HOME)
    assert result.bucket == "filtered"
    assert "anchor" in result.reason
    assert result.legs == ()


def test_meeting_at_home_matches_case_and_whitespace_insensitively():
    """Same deterministic equality as the neighbour rule — no geocode."""
    start = NOW + timedelta(hours=3)
    event = _meeting(
        "evt_home",
        start=start,
        end=start + timedelta(hours=1),
        location="  12 example st,\n Sampleton,   TN 37000 ",
    )
    [result] = scan([event], now=NOW, home_address=HOME)
    assert result.bucket == "filtered"
    assert result.legs == ()


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("12 Example St, Sampleton", "  12 example st,\n Sampleton ", True),
        ("12 Example St", "14 Example St", False),
        ("12 Example St", None, False),
        (None, None, False),
    ],
)
def test_same_place_normalizes_both_sides(a, b, same):
    assert same_place(a, b) is same


def test_meeting_at_the_lodging_is_filtered_on_a_trip():
    """On a trip the anchor is the current lodging; a meeting held there is the
    same nowhere-to-drive case."""
    start = NOW + timedelta(days=1)
    event = _meeting("evt_hotel", start=start, end=start + timedelta(hours=1), location=LODGING)
    [result] = scan([event], now=NOW, home_address=HOME, anchor_for=lambda _at: (LODGING, None))
    assert result.bucket == "filtered"
    assert "anchor" in result.reason
    assert result.legs == ()


def test_meeting_at_home_does_not_disturb_a_later_meeting_elsewhere():
    """A home appointment followed 30 min later by a real meeting: the real
    meeting still drives out from home, the home one stays filtered."""
    home_start = NOW + timedelta(hours=3)
    away_start = home_start + timedelta(hours=1, minutes=30)  # 30-min gap: tight
    events = [
        _meeting("evt_home", start=home_start, end=home_start + timedelta(hours=1), location=HOME),
        _meeting("evt_away", start=away_start, end=away_start + timedelta(hours=1)),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))
    assert by_id["evt_home"].bucket == "filtered"
    assert by_id["evt_away"].bucket == "needs_decision"
    assert [leg.direction for leg in by_id["evt_away"].legs] == ["outbound", "return"]
    assert by_id["evt_away"].legs[0].origin == HOME


def test_meeting_at_home_is_not_a_tight_gap_neighbour():
    """A venue meeting ending 14:00 then a home appointment at 14:30: the venue
    meeting keeps its own return leg home. Before #301 the home event acted as
    a tight-gap neighbour — the return was dropped and the drive home became a
    "bridge" to an appointment at the house."""
    venue_start = NOW + timedelta(hours=3)
    venue_end = venue_start + timedelta(hours=1)
    home_start = venue_end + timedelta(minutes=30)  # tight
    events = [
        _meeting("evt_venue", start=venue_start, end=venue_end),
        _meeting("evt_home", start=home_start, end=home_start + timedelta(hours=1), location=HOME),
    ]
    by_id = _by_id(scan(events, now=NOW, home_address=HOME))
    assert by_id["evt_venue"].bucket == "needs_decision"
    directions = [leg.direction for leg in by_id["evt_venue"].legs]
    assert directions == ["outbound", "return"]
    ret = by_id["evt_venue"].legs[1]
    assert ret.destination == HOME
    assert by_id["evt_home"].bucket == "filtered"
    assert by_id["evt_home"].legs == ()


# --- #284: a declared timeZone that contradicts its own dateTime offset ----
#
# Asserted through the public `scan()` API and the timezone it puts on the
# returned MeetingClass — that value is what `calendar_apply` renders the
# operator notice from, so it is the outcome, not an internal. The render
# itself is calendar_apply's to test: reaching into `_start_in_local` here
# would trade one private-helper assertion for another.
#
# `Etc/GMT+7` is the POSIX-inverted spelling of UTC-07:00, so a block
# carrying it renders the 16:00Z instant as the 09:00 the invite meant.


def _tz_meeting(dt_iso: str, tz: str | None) -> dict:
    """A future timed meeting whose start block carries an explicit offset
    in `dateTime` and, optionally, a separately-declared `timeZone`."""
    start_block: dict = {"dateTime": dt_iso}
    end_block: dict = {
        "dateTime": (datetime.fromisoformat(dt_iso) + timedelta(hours=1)).isoformat()
    }
    if tz is not None:
        start_block["timeZone"] = tz
        end_block["timeZone"] = tz
    return {
        "id": "tz1",
        "summary": "Zero Downtime Hackathon",
        "description": "",
        "location": "625 2nd St, San Francisco, CA 94107",
        "start": start_block,
        "end": end_block,
    }


def _scanned_tz(dt_iso: str, tz: str | None, *, now: datetime = NOW) -> str | None:
    [result] = scan([_tz_meeting(dt_iso, tz)], now=now, home_address=HOME)
    return result.timezone


def test_scan_distrusts_a_timezone_contradicting_its_offset():
    """The #284 live event. A Luma/Partiful-style import wrote a Pacific
    wall-clock and stamped `timeZone: "UTC"`. "UTC" is valid IANA, so the
    unresolvable-zone fallback never trips and the notice renders the
    meeting faithfully in the wrong zone — 09:00 PDT announced as 16:00."""
    assert _scanned_tz("2026-08-22T09:00:00-07:00", "UTC") == "Etc/GMT+7"


def test_scan_keeps_a_timezone_agreeing_with_its_offset():
    """The overwhelmingly common case must be untouched: a real Google event
    whose IANA name and offset describe the same instant. -05:00 is what
    America/Chicago is on that August date (CDT), so the name is kept."""
    assert _scanned_tz("2026-08-22T09:00:00-05:00", "America/Chicago") == "America/Chicago"


def test_scan_keeps_genuine_utc():
    """A zero offset declared as UTC is not a contradiction."""
    assert _scanned_tz("2026-08-22T16:00:00+00:00", "UTC") == "UTC"


def test_scan_keeps_dst_correct_names_across_the_boundary():
    """The check compares the zone's offset AT THAT INSTANT, not a fixed
    one, so the same IANA name survives on both sides of a DST change.

    Both instants are derived from the pinned `NOW` by fixed offsets, so
    neither is a future-date literal (`coding-policy: testing-standards`
    Determinism). The winter case needs its own injected clock too:
    `scan` filters a meeting starting before the `now` it is given, so a
    January meeting cannot be scanned against a July `now`."""
    # The module-level `CT` is a FIXED -05:00 offset, so it can never
    # express CST. This test needs the real DST-aware zone.
    chicago = ZoneInfo("America/Chicago")
    winter_now = (NOW - timedelta(days=180)).astimezone(chicago)  # early Jan
    winter_meeting = (winter_now + timedelta(days=13)).astimezone(chicago)
    assert winter_meeting.utcoffset() == timedelta(hours=-6)  # CST, not CDT
    assert (
        _scanned_tz(winter_meeting.isoformat(), "America/Chicago", now=winter_now)
        == "America/Chicago"
    )

    summer_meeting = (NOW + timedelta(days=14)).astimezone(chicago)  # mid-July
    assert summer_meeting.utcoffset() == timedelta(hours=-5)  # CDT
    assert _scanned_tz(summer_meeting.isoformat(), "America/Chicago") == "America/Chicago"


def test_scan_falls_back_to_offset_when_no_timezone_declared():
    """Unchanged pre-existing behaviour: no `timeZone`, derive from offset."""
    assert _scanned_tz("2026-08-22T09:00:00-07:00", None) == "Etc/GMT+7"


def test_scan_prefers_the_offset_over_an_unresolvable_name():
    """An unresolvable name is not merely uncheckable, it is unusable:
    `_start_in_local` cannot resolve it either and falls back to UTC,
    rendering the 09:00 PDT meeting as 16:00 — the very bug this guard
    exists to stop. Caught in review; the first draft kept the name."""
    assert _scanned_tz("2026-08-22T09:00:00-07:00", "Mars/Phobos") == "Etc/GMT+7"


def test_scan_keeps_an_unresolvable_name_with_no_etc_mapping():
    """The one case where an unresolvable name survives: a non-whole-hour
    offset has no `Etc/GMT±N` to fall back to, so there is nothing better
    to offer."""
    assert _scanned_tz("2026-08-22T09:00:00+05:30", "Mars/Phobos") == "Mars/Phobos"


def test_scan_survives_a_boundary_datetime_that_overflows_conversion():
    """`_parse_event`'s contract is that one malformed event never aborts a
    wide-window sweep. `0001-01-01T00:00:00+14:00` is syntactically valid,
    but converting it into a named zone walks past `datetime.min` and
    raises OverflowError — so the trust check treats it as untrustworthy
    rather than letting it propagate and kill the scan. Caught in review."""
    events = [_tz_meeting("0001-01-01T00:00:00+14:00", "America/Chicago")]
    [result] = scan(events, now=NOW, home_address=HOME)
    # The event is millennia in the past, so it buckets as `past` — the
    # point is that the sweep reaches a verdict at all instead of raising.
    assert result.bucket == "past"
