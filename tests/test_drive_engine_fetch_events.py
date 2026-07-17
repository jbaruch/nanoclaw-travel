"""Tests for the drive-engine calendar fetch (`fetch_events.py`).

Mocks `urllib.request.urlopen` so the tests exercise request shaping, event
extraction, projection to the scan-event fields, and input guards — without
touching the live Google Calendar API. Synthetic fixtures only (no real
calendar IDs). A final check confirms the projected events are exactly what
`scan()` consumes.

The transport, pagination and error taxonomy now live in
`google_calendar_client` (#638 folded this module's duplicate Composio
transport onto it), so they are tested once, there. What is tested here is what
this module still owns: the window contract and the projection.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from fetch_events import CalendarFetcher, GoogleCalendarError  # noqa: E402
from scan import scan  # noqa: E402

CT = timezone(timedelta(hours=-5))
NOW = datetime(2026, 7, 1, 8, 0, tzinfo=CT)
LATER = NOW + timedelta(days=14)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _ok(payload: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode())


def _fetcher() -> CalendarFetcher:
    return CalendarFetcher()


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _event(eid: str) -> dict:
    return {
        "id": eid,
        "summary": "Customer sync",
        "location": "100 Broadway, Nashville, TN",
        "start": {"dateTime": "2026-07-02T13:00:00-05:00"},
        "end": {"dateTime": "2026-07-02T14:00:00-05:00"},
        "description": "",
        "etag": "drop-me",  # extra GCal field that must be projected away
    }


# --- request shaping ------------------------------------------------------


def test_fetch_calls_events_list_with_window_args():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = {k.lower() for k in request.headers}
        return _ok({"items": []})

    with patch("urllib.request.urlopen", fake_urlopen):
        _fetcher().fetch_window(time_min=NOW, time_max=LATER)

    path = urllib.parse.urlsplit(captured["url"]).path
    assert path == "/calendar/v3/calendars/primary/events"
    args = _query(captured["url"])
    # singleEvents expands recurrences so a weekly standup surfaces as instances
    assert args["singleEvents"] == "true"
    assert args["timeMin"] == NOW.isoformat()
    assert args["timeMax"] == LATER.isoformat()
    # #638: no credential in this container — the gateway injects the Bearer.
    assert "authorization" not in captured["headers"]
    assert "x-api-key" not in captured["headers"]


def test_fetch_needs_no_composio_env(monkeypatch):
    """The COMPOSIO_* vars are gone; the fetch must work without them."""
    for var in ("COMPOSIO_API_KEY", "COMPOSIO_USER_ID", "COMPOSIO_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"items": [_event("a")]})):
        assert [e["id"] for e in _fetcher().fetch_window(time_min=NOW, time_max=LATER)] == ["a"]


def test_base_url_env_override_reaches_the_fetch(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_API_BASE", "https://calendar.example/calendar/v3")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _ok({"items": []})

    with patch("urllib.request.urlopen", fake_urlopen):
        _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert captured["url"].startswith("https://calendar.example/calendar/v3")


# --- pagination (#171) ----------------------------------------------------


def _page(events: list, token: str | None = None) -> _FakeResponse:
    payload: dict = {"items": events}
    if token is not None:
        payload["nextPageToken"] = token
    return _ok(payload)


def test_fetch_requests_max_page_size():
    # Without maxResults events.list caps at Google's default 250 and silently
    # truncates a busy calendar (#171); the fetch must ask for the max page.
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["args"] = _query(request.full_url)
        return _page([_event("a")])

    with patch("urllib.request.urlopen", fake_urlopen):
        _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert captured["args"]["maxResults"] == "2500"


def test_drains_all_pages_following_next_page_token():
    # The core storm fix (#171): every page in the window is followed and
    # accumulated, so the caller scans the complete calendar, not the first 250.
    pages = [
        _page([_event("a"), _event("b")], token="tok-2"),
        _page([_event("c")], token="tok-3"),
        _page([_event("d")]),  # terminal page, no token
    ]
    sent_tokens = []

    def fake_urlopen(request, timeout=None):
        sent_tokens.append(_query(request.full_url).get("pageToken"))
        return pages[len(sent_tokens) - 1]

    with patch("urllib.request.urlopen", fake_urlopen):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert sent_tokens == [None, "tok-2", "tok-3"]
    assert [e["id"] for e in events] == ["a", "b", "c", "d"]


def test_non_clearing_token_is_bounded_not_infinite():
    def fake_urlopen(request, timeout=None):
        return _page([_event("a")], token="always-more")

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(GoogleCalendarError, match="did not drain within"):
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)


# --- event extraction + projection ---------------------------------------


def test_extracts_items():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"items": [_event("b")]})):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert [e["id"] for e in events] == ["b"]


def test_projection_drops_extra_fields():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"items": [_event("a")]})):
        [event] = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert "etag" not in event
    assert set(event) == {"id", "summary", "location", "start", "end", "description"}


def test_projection_carries_attendees_and_status_for_decline_filter():
    # scan.py reads the operator's RSVP (attendees) + event status to skip
    # declined / cancelled meetings; the fetch must carry both through.
    raw = _event("a")
    raw["attendees"] = [{"self": True, "responseStatus": "declined"}]
    raw["status"] = "confirmed"
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"items": [raw]})):
        [event] = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert event["attendees"][0]["responseStatus"] == "declined"
    assert event["status"] == "confirmed"


def test_projection_carries_description_for_recheck_poll():
    # The recheck poll reads the block's machine state back out of the
    # `description` (that is where calendar-as-state lives), so the fetch must
    # carry `description` through verbatim.
    raw = _event("a")
    raw["description"] = 'Drive: X\n[drive-planner:meeting=evt_1:dir=outbound]\n<!--dp:{"v":1}-->'
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"items": [raw]})):
        [event] = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert event["description"] == raw["description"]


def test_empty_window_returns_empty_list():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"items": []})):
        assert _fetcher().fetch_window(time_min=NOW, time_max=LATER) == []


def test_non_dict_events_are_preserved_for_scan_not_silently_dropped():
    # A malformed (non-dict) entry must survive the fetch so scan() can
    # surface it as `filtered` — dropping it here would hide a partial
    # response-shape regression as an invisible empty sweep.
    payload = {"items": [_event("a"), "garbage", 123]}
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok(payload)):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert len(events) == 3
    assert events[1] == "garbage" and events[2] == 123
    # and scan classifies them without crashing
    results = scan(events, now=NOW, home_address="Home")
    buckets = sorted(r.bucket for r in results)
    assert buckets == ["filtered", "filtered", "needs_decision"]


# --- failure modes --------------------------------------------------------


def test_response_without_items_is_an_empty_window():
    """Under Composio a missing event list meant "shape regression" and raised,
    because the list could live under any of several keys. events.list has one
    shape, so no `items` genuinely means no events."""
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({})):
        assert _fetcher().fetch_window(time_min=NOW, time_max=LATER) == []


def test_api_failure_propagates_as_google_calendar_error():
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(GoogleCalendarError) as exc:
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert exc.value.status_code == 404


def test_read_timeout_normalized_to_urlerror():
    def fake_urlopen(request, timeout=None):
        raise TimeoutError("read timed out")

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(urllib.error.URLError):
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)


# --- input guards ---------------------------------------------------------


def test_naive_window_raises():
    with pytest.raises(ValueError, match="timezone-aware"):
        _fetcher().fetch_window(time_min=datetime(2026, 7, 1, 8, 0), time_max=LATER)


def test_inverted_window_raises():
    with pytest.raises(ValueError, match="after time_min"):
        _fetcher().fetch_window(time_min=LATER, time_max=NOW)


def test_injected_client_is_used():
    """The fetcher takes a client so a caller can share one per process."""

    class _Stub:
        def __init__(self):
            self.calls = []

        def find_events(self, arguments):
            self.calls.append(arguments)
            return {"items": [_event("z")]}

    stub = _Stub()
    events = CalendarFetcher(client=stub).fetch_window(time_min=NOW, time_max=LATER)
    assert [e["id"] for e in events] == ["z"]
    assert stub.calls[0]["calendar_id"] == "primary"


# --- integration: fetched events are scan-compatible ---------------------


def test_fetched_events_feed_scan():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"items": [_event("a")]})):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    [result] = scan(events, now=NOW, home_address="Home")
    assert result.meeting_id == "a"
    assert result.bucket == "needs_decision"
