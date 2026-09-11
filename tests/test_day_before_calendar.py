"""Tests for the day-before calendar list (#300): `day_before_calendar.py` and
`scripts/day-before-calendar.py`.

Deterministic fixtures only — hand-built Google Calendar event dicts, fixed
instants, a fake calendar client, an injected zone reader. Pins what the
compose used to decide on its own:

  - an event the operator declined, or one its organizer cancelled, is not
    listed (the declined football practice in #300's notification)
  - every listed time is on the operator's clock, whatever offset the source
    event carried (the stale `+02:00` on an `America/Chicago` event)
  - with no operator zone, each event keeps its own offset and `tz` is null
  - the window follows the flight's effective departure/arrival
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))

from day_before_calendar import (  # noqa: E402
    CONFLICT_MARGIN,
    calendar_conflicts,
    conflict_window,
    resolve_zone,
)
from google_calendar_client import (  # noqa: E402
    GatewayNotInjecting,
    GoogleCalendarError,
    TierAccessRestricted,
)
from operator_tz import OperatorTz  # noqa: E402

CHICAGO = "America/Chicago"
UTC = timezone.utc

# DL1144 MSP→BNA from #300: dep 19:50 CDT, arr 21:47 CDT on 2026-09-10.
_STATE = {
    "flight_id": 7356314,
    "scheduled_dep_time": "2026-09-10T19:50:00-05:00",
    "scheduled_arr_time": "2026-09-10T21:47:00-05:00",
    "last_snapshot": {"dep_time": None, "arr_time": None},
}
_WINDOW = (
    datetime(2026, 9, 10, 16, 50, tzinfo=timezone(timedelta(hours=-5))),
    datetime(2026, 9, 11, 0, 47, tzinfo=timezone(timedelta(hours=-5))),
)


def _event(
    summary: str,
    start: str,
    end: str,
    *,
    declined: bool = False,
    status: str = "confirmed",
    location: str | None = None,
) -> dict:
    event: dict = {
        "id": summary.lower().replace(" ", "-"),
        "summary": summary,
        "status": status,
        "start": {"dateTime": start, "timeZone": CHICAGO},
        "end": {"dateTime": end, "timeZone": CHICAGO},
        "attendees": [
            {"email": "coach@example.com", "responseStatus": "accepted"},
            {
                "email": "operator@example.com",
                "self": True,
                "responseStatus": "declined" if declined else "accepted",
            },
        ],
    }
    if location is not None:
        event["location"] = location
    return event


# --- the window ---------------------------------------------------------------


def test_window_spans_the_margin_around_scheduled_times():
    start, end = conflict_window(_STATE)
    assert start == datetime(2026, 9, 10, 19, 50, tzinfo=timezone(timedelta(hours=-5))) - (
        CONFLICT_MARGIN
    )
    assert end == datetime(2026, 9, 10, 21, 47, tzinfo=timezone(timedelta(hours=-5))) + (
        CONFLICT_MARGIN
    )


def test_window_follows_a_published_live_departure():
    state = {
        **_STATE,
        "last_snapshot": {
            "dep_time": "2026-09-10T21:00:00-05:00",
            "arr_time": "2026-09-10T22:57:00-05:00",
        },
    }
    start, end = conflict_window(state)
    assert start == datetime(2026, 9, 10, 18, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert end == datetime(2026, 9, 11, 1, 57, tzinfo=timezone(timedelta(hours=-5)))


def test_window_rejects_an_unparseable_departure():
    with pytest.raises(ValueError, match="no parseable departure"):
        conflict_window({**_STATE, "scheduled_dep_time": "not-a-time"})


# --- which events count -------------------------------------------------------


def test_declined_event_is_not_listed_and_is_counted():
    """#300: the operator's declined practice was framed as a conflict."""
    events = [
        _event(
            "Football practice",
            "2026-09-10T23:00:00+02:00",
            "2026-09-11T01:10:00+02:00",
            declined=True,
        ),
        _event("Dinner", "2026-09-10T18:00:00-05:00", "2026-09-10T19:00:00-05:00"),
    ]
    listed, skipped = calendar_conflicts(events, window=_WINDOW, zone=ZoneInfo(CHICAGO))
    assert [e["summary"] for e in listed] == ["Dinner"]
    assert skipped == 1


def test_cancelled_event_is_not_listed():
    events = [
        _event(
            "Standup",
            "2026-09-10T18:00:00-05:00",
            "2026-09-10T18:30:00-05:00",
            status="cancelled",
        )
    ]
    listed, skipped = calendar_conflicts(events, window=_WINDOW, zone=ZoneInfo(CHICAGO))
    assert listed == []
    assert skipped == 1


def test_tentative_and_needs_action_events_still_count():
    event = _event("Sync", "2026-09-10T18:00:00-05:00", "2026-09-10T18:30:00-05:00")
    event["attendees"][1]["responseStatus"] = "tentative"
    no_rsvp = _event("Call", "2026-09-10T20:00:00-05:00", "2026-09-10T20:30:00-05:00")
    no_rsvp["attendees"][1]["responseStatus"] = "needsAction"
    listed, _ = calendar_conflicts([event, no_rsvp], window=_WINDOW, zone=ZoneInfo(CHICAGO))
    assert [e["summary"] for e in listed] == ["Sync", "Call"]


def test_event_outside_the_window_is_not_listed():
    events = [_event("Lunch", "2026-09-10T12:00:00-05:00", "2026-09-10T13:00:00-05:00")]
    listed, skipped = calendar_conflicts(events, window=_WINDOW, zone=ZoneInfo(CHICAGO))
    assert listed == []
    assert skipped == 0


def test_unparseable_event_is_left_out():
    broken = {"summary": "Ghost", "start": {"dateTime": "soon"}, "end": {"dateTime": "later"}}
    rows: list = [broken, "junk"]  # a non-dict row must be skipped, not raise
    listed, skipped = calendar_conflicts(rows, window=_WINDOW, zone=ZoneInfo(CHICAGO))
    assert listed == []
    assert skipped == 0


# --- what clock the times read on ---------------------------------------------


def test_stale_offset_is_rendered_on_the_operator_clock():
    """The #300/#301 artifact: a `+02:00` offset on an `America/Chicago` event.
    The instant is authoritative: 02:00+02:00 on Sep 11 is 19:00 CDT on Sep 10,
    a different calendar day on each clock."""
    events = [_event("Pickup", "2026-09-11T02:00:00+02:00", "2026-09-11T02:45:00+02:00")]
    listed, _ = calendar_conflicts(events, window=_WINDOW, zone=ZoneInfo(CHICAGO))
    [entry] = listed
    assert entry["start"] == "2026-09-10T19:00:00-05:00"
    assert entry["end"] == "2026-09-10T19:45:00-05:00"
    assert entry["display"] == "Thu Sep 10, 19:00–19:45"


def test_event_just_before_the_window_is_not_listed():
    """23:00+02:00 is 16:00 CDT, ten minutes before the 16:50 window opens."""
    events = [_event("Pickup", "2026-09-10T23:00:00+02:00", "2026-09-10T23:45:00+02:00")]
    listed, _ = calendar_conflicts(events, window=_WINDOW, zone=ZoneInfo(CHICAGO))
    assert listed == []


def test_no_operator_zone_keeps_each_event_offset():
    events = [_event("Dinner", "2026-09-10T18:00:00-05:00", "2026-09-10T19:00:00-05:00")]
    listed, _ = calendar_conflicts(events, window=_WINDOW, zone=None)
    assert listed[0]["start"] == "2026-09-10T18:00:00-05:00"


def test_no_zone_display_names_the_source_offset():
    """Copilot on #307: without an operator zone the display must say whose
    clock it is on, so a stale `+02:00` is never read as operator-local."""
    events = [_event("Pickup", "2026-09-11T02:00:00+02:00", "2026-09-11T02:45:00+02:00")]
    listed, _ = calendar_conflicts(events, window=_WINDOW, zone=None)
    assert listed[0]["display"] == "Fri Sep 11, 02:00–02:45 (UTC+02:00)"


def test_unresolvable_zone_resolves_to_none_and_says_so(capsys):
    assert resolve_zone("Mars/Olympus_Mons") is None
    assert "Mars/Olympus_Mons" in capsys.readouterr().err
    assert resolve_zone(None) is None
    assert resolve_zone(CHICAGO) == ZoneInfo(CHICAGO)


def test_all_day_event_is_listed_by_date():
    event = {
        "summary": "School holiday",
        "start": {"date": "2026-09-10"},
        "end": {"date": "2026-09-11"},
    }
    listed, _ = calendar_conflicts([event], window=_WINDOW, zone=ZoneInfo(CHICAGO))
    [entry] = listed
    assert entry["all_day"] is True
    assert entry["start"] == "2026-09-10"
    assert entry["display"] == "Thu Sep 10 (all day)"


def test_all_day_event_counts_on_its_local_day_in_a_late_evening_window():
    """Policy review on #307: a Sep 10 all-day event built at UTC midnight
    ends 19:00 CDT Sep 10 and misses a 20:00–23:00 CDT window that evening.
    Read in the operator's zone it spans all of Sep 10 and is listed."""
    evening = (
        datetime(2026, 9, 10, 20, 0, tzinfo=ZoneInfo(CHICAGO)),
        datetime(2026, 9, 10, 23, 0, tzinfo=ZoneInfo(CHICAGO)),
    )
    event = {
        "summary": "School holiday",
        "start": {"date": "2026-09-10"},
        "end": {"date": "2026-09-11"},
    }
    listed, _ = calendar_conflicts([event], window=evening, zone=ZoneInfo(CHICAGO))
    assert [e["summary"] for e in listed] == ["School holiday"]


def test_all_day_event_on_the_next_day_misses_an_evening_window():
    evening = (
        datetime(2026, 9, 10, 20, 0, tzinfo=ZoneInfo(CHICAGO)),
        datetime(2026, 9, 10, 23, 0, tzinfo=ZoneInfo(CHICAGO)),
    )
    event = {
        "summary": "Field trip",
        "start": {"date": "2026-09-11"},
        "end": {"date": "2026-09-12"},
    }
    listed, _ = calendar_conflicts([event], window=evening, zone=ZoneInfo(CHICAGO))
    assert listed == []


def test_all_day_days_follow_the_window_offset_without_a_zone():
    """No operator zone: an all-day day is read in the window's own offset
    (the flight's), still never at UTC midnight."""
    evening = (
        datetime(2026, 9, 10, 20, 0, tzinfo=timezone(timedelta(hours=-5))),
        datetime(2026, 9, 10, 23, 0, tzinfo=timezone(timedelta(hours=-5))),
    )
    event = {
        "summary": "School holiday",
        "start": {"date": "2026-09-10"},
        "end": {"date": "2026-09-11"},
    }
    listed, _ = calendar_conflicts([event], window=evening, zone=None)
    assert [e["summary"] for e in listed] == ["School holiday"]


def test_listed_events_are_ordered_by_start_and_carry_location():
    events = [
        _event("Late", "2026-09-10T21:00:00-05:00", "2026-09-10T21:30:00-05:00"),
        _event(
            "Early",
            "2026-09-10T17:00:00-05:00",
            "2026-09-10T17:30:00-05:00",
            location="1 Main St",
        ),
    ]
    listed, _ = calendar_conflicts(events, window=_WINDOW, zone=ZoneInfo(CHICAGO))
    assert [(e["summary"], e["location"]) for e in listed] == [
        ("Early", "1 Main St"),
        ("Late", None),
    ]


# --- the script ---------------------------------------------------------------


def _load_script():
    path = REPO_ROOT / "skills" / "flight-assist" / "scripts" / "day-before-calendar.py"
    spec = importlib.util.spec_from_file_location("day_before_calendar_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCalendar:
    def __init__(self, *, items=None, raises=None):
        self.items = items or []
        self.raises = raises
        self.calls: list[dict] = []

    def find_events(self, arguments: dict) -> dict:
        self.calls.append(arguments)
        if self.raises is not None:
            raise self.raises
        return {"items": self.items}


def _reader(tz: str | None):
    if tz is None:
        return lambda: None
    return lambda: OperatorTz(tz=tz, local_now="x", local_date="y")


def _run(capsys, *, client, tz: str | None = CHICAGO, state: dict | None = _STATE):
    script = _load_script()
    code = script.run(
        7356314,
        client=client,
        operator_tz_reader=_reader(tz),
        read_state=lambda _flight_id: state,
    )
    out = capsys.readouterr()
    return code, json.loads(out.out), out.err


def test_script_lists_filtered_events_on_the_operator_clock(capsys):
    client = FakeCalendar(
        items=[
            _event(
                "Football practice",
                "2026-09-10T23:00:00+02:00",
                "2026-09-11T01:10:00+02:00",
                declined=True,
            ),
            _event("Pickup", "2026-09-11T02:00:00+02:00", "2026-09-11T02:45:00+02:00"),
        ]
    )
    code, payload, _ = _run(capsys, client=client)
    assert code == 0
    assert payload["tz"] == CHICAGO
    assert [e["display"] for e in payload["events"]] == ["Thu Sep 10, 19:00–19:45"]
    assert payload["skipped_declined_or_cancelled"] == 1
    [args] = client.calls
    assert args["calendar_id"] == "primary"
    assert args["singleEvents"] is True
    assert args["timeMin"] == _WINDOW[0].isoformat()
    assert args["timeMax"] == _WINDOW[1].isoformat()


def test_script_without_a_zone_reports_null_tz(capsys):
    client = FakeCalendar(
        items=[_event("Dinner", "2026-09-10T18:00:00-05:00", "2026-09-10T19:00:00-05:00")]
    )
    code, payload, _ = _run(capsys, client=client, tz=None)
    assert code == 0
    assert payload["tz"] is None
    assert payload["events"][0]["start"] == "2026-09-10T18:00:00-05:00"


def test_script_reports_an_unresolvable_zone_as_null(capsys):
    """Copilot on #307: the payload's `tz` names the zone the times were
    rendered in. A reader name that does not resolve rendered nothing, so the
    compose must not read the raw offsets as operator-local."""
    client = FakeCalendar(
        items=[_event("Dinner", "2026-09-10T18:00:00-05:00", "2026-09-10T19:00:00-05:00")]
    )
    code, payload, err = _run(capsys, client=client, tz="Mars/Olympus_Mons")
    assert code == 0
    assert payload["tz"] is None
    assert payload["events"][0]["start"] == "2026-09-10T18:00:00-05:00"
    assert "Mars/Olympus_Mons" in err


def test_script_with_an_unparseable_window_returns_a_state_error(capsys):
    """Policy review on #307: a malformed persisted time must yield the
    documented error result, never a traceback with no JSON."""
    client = FakeCalendar()
    code, payload, err = _run(capsys, client=client, state={**_STATE, "scheduled_arr_time": ""})
    assert code == 1
    assert payload == {"error": "state"}
    assert "no parseable departure/arrival" in err
    assert client.calls == []


def test_script_with_no_state_fails_with_a_rerun_command(capsys):
    """Policy review on #307: a missing state record is a failure, reported on
    stderr with what to do, not a quiet exit 0."""
    code, payload, err = _run(capsys, client=FakeCalendar(), state=None)
    assert code == 1
    assert payload == {"error": "no_state"}
    assert "no state on disk" in err
    assert "day-before-calendar.py 7356314" in err


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (GatewayNotInjecting("401"), "gateway"),
        (GoogleCalendarError("500", status_code=500), "calendar"),
        (urllib.error.URLError("timed out"), "calendar"),
        (json.JSONDecodeError("Expecting value", "<html>", 0), "calendar"),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), "calendar"),
    ],
)
def test_script_calendar_failures_exit_1_with_a_named_error(capsys, exc, error):
    code, payload, err = _run(capsys, client=FakeCalendar(raises=exc))
    assert code == 1
    assert payload == {"error": error}
    # Nothing retries a fired day_before; the message names the rerun command
    # (policy review on #307).
    assert "day-before-calendar.py 7356314" in err
    assert "next wake" not in err


def test_script_tier_gate_exits_1_without_a_rerun_command(capsys):
    """The tier is gated from Google by design; a rerun would fail the same way."""
    code, payload, err = _run(
        capsys, client=FakeCalendar(raises=TierAccessRestricted("403 access_restricted"))
    )
    assert code == 1
    assert payload == {"error": "tier"}
    assert "tier with Google access" in err
    assert "rerun" not in err


def test_script_usage_errors_exit_2(capsys):
    script = _load_script()
    assert script.main(["day-before-calendar.py"]) == 2
    assert script.main(["day-before-calendar.py", "DL1144"]) == 2
    assert "flight_id must be an int" in capsys.readouterr().err
