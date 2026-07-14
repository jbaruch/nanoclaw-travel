"""Tests for the drive-planner calendar fetch (`fetch_events.py`).

Mocks `urllib.request.urlopen` so the tests exercise request shaping, the
Composio success/failure envelope, event extraction across the candidate
container shapes, projection to the scan-event fields, and input guards —
without touching the live Composio backend. Synthetic fixtures only (no real
keys, no real calendar IDs). A final check confirms the projected events are
exactly what `scan()` consumes.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-planner"))

from fetch_events import (  # noqa: E402
    ACTION_LIST_EVENTS,
    CalendarFetcher,
    FetchError,
)
from scan import scan  # noqa: E402

SYNTH_KEY = "synthetic_composio_key"
SYNTH_USER = "synthetic_user_42"
SYNTH_BASE = "https://composio.example/api/v3"

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


def _ok(data: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps({"data": data, "successful": True, "error": None}).encode())


def _fail(error: str, status_code: int | None = None) -> _FakeResponse:
    data = {"status_code": status_code} if status_code is not None else {}
    return _FakeResponse(json.dumps({"data": data, "successful": False, "error": error}).encode())


def _fetcher() -> CalendarFetcher:
    return CalendarFetcher(SYNTH_KEY, SYNTH_USER, base_url=SYNTH_BASE)


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


def test_fetch_posts_action_with_window_args():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return _ok({"events": []})

    with patch("urllib.request.urlopen", fake_urlopen):
        _fetcher().fetch_window(time_min=NOW, time_max=LATER)

    assert captured["url"] == f"{SYNTH_BASE}/tools/execute/{ACTION_LIST_EVENTS}"
    assert ACTION_LIST_EVENTS == "GOOGLECALENDAR_EVENTS_LIST"
    assert captured["body"]["user_id"] == SYNTH_USER
    args = captured["body"]["arguments"]
    # the v3 schema requires calendarId; singleEvents expands recurrences
    assert args["calendarId"] == "primary"
    assert args["singleEvents"] is True
    assert args["timeMin"] == NOW.isoformat()
    assert args["timeMax"] == LATER.isoformat()
    assert captured["headers"]["x-api-key"] == SYNTH_KEY


# --- pagination (#171) ----------------------------------------------------


def _page(events: list, token: str | None = None) -> _FakeResponse:
    data: dict = {"items": events}
    if token is not None:
        data["nextPageToken"] = token
    return _ok(data)


def test_fetch_requests_max_page_size():
    # Without maxResults the action caps at Google's default 250 and silently
    # truncates a busy calendar (#171); the fetch must ask for the max page.
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["args"] = json.loads(request.data.decode())["arguments"]
        return _page([_event("a")])

    with patch("urllib.request.urlopen", fake_urlopen):
        _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert captured["args"]["maxResults"] == 2500


def test_single_page_when_no_token():
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(json.loads(request.data.decode())["arguments"].get("pageToken"))
        return _page([_event("a"), _event("b")])

    with patch("urllib.request.urlopen", fake_urlopen):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert calls == [None]
    assert [e["id"] for e in events] == ["a", "b"]


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
        sent_tokens.append(json.loads(request.data.decode())["arguments"].get("pageToken"))
        return pages[len(sent_tokens) - 1]

    with patch("urllib.request.urlopen", fake_urlopen):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert sent_tokens == [None, "tok-2", "tok-3"]
    assert [e["id"] for e in events] == ["a", "b", "c", "d"]


def test_non_clearing_token_is_bounded_not_infinite():
    def fake_urlopen(request, timeout=None):
        return _page([_event("a")], token="always-more")

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(FetchError, match="did not drain within"):
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)


# --- event extraction + projection ---------------------------------------


def test_extracts_events_container():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"events": [_event("a")]})):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert [e["id"] for e in events] == ["a"]


def test_extracts_items_container():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"items": [_event("b")]})):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert [e["id"] for e in events] == ["b"]


def test_projection_drops_extra_fields():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"events": [_event("a")]})):
        [event] = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert "etag" not in event
    assert set(event) == {"id", "summary", "location", "start", "end", "description"}


def test_projection_carries_attendees_and_status_for_decline_filter():
    # scan.py reads the operator's RSVP (attendees) + event status to skip
    # declined / cancelled meetings; the fetch must carry both through.
    raw = _event("a")
    raw["attendees"] = [{"self": True, "responseStatus": "declined"}]
    raw["status"] = "confirmed"
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"events": [raw]})):
        [event] = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert event["attendees"][0]["responseStatus"] == "declined"
    assert event["status"] == "confirmed"


def test_projection_carries_description_for_recheck_poll():
    # The recheck poll reads the block's machine state back out of the
    # `description` (the live v3 toolkit has no writable extendedProperties), so
    # the fetch must carry `description` through verbatim.
    raw = _event("a")
    raw["description"] = 'Drive: X\n[drive-planner:meeting=evt_1:dir=outbound]\n<!--dp:{"v":1}-->'
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"events": [raw]})):
        [event] = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert event["description"] == raw["description"]


def test_extracts_events_nested_under_response_data():
    # Some toolkit shapes wrap the Google payload one level under
    # `response_data`; the fetch must still find the list, not raise.
    payload = {"response_data": {"items": [_event("n")]}}
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok(payload)):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert [e["id"] for e in events] == ["n"]


def test_empty_window_returns_empty_list():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"events": []})):
        assert _fetcher().fetch_window(time_min=NOW, time_max=LATER) == []


def test_non_dict_events_are_preserved_for_scan_not_silently_dropped():
    # A malformed (non-dict) entry must survive the fetch so scan() can
    # surface it as `filtered` — dropping it here would hide a partial
    # response-shape regression as an invisible empty sweep.
    payload = {"events": [_event("a"), "garbage", 123]}
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok(payload)):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert len(events) == 3
    assert events[1] == "garbage" and events[2] == 123
    # and scan classifies them without crashing
    results = scan(events, now=NOW, home_address="Home")
    buckets = sorted(r.bucket for r in results)
    assert buckets == ["filtered", "filtered", "needs_decision"]


# --- failure modes --------------------------------------------------------


def test_tool_level_failure_raises_fetch_error():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _fail("calendar not connected")):
        with pytest.raises(FetchError, match="calendar not connected"):
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)


def test_failure_surfaces_status_code():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _fail("nope", status_code=403)):
        with pytest.raises(FetchError) as exc:
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    assert exc.value.status_code == 403


def test_unrecognized_shape_raises_rather_than_silent_empty():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"surprise": []})):
        with pytest.raises(FetchError, match="no event list found"):
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)


def test_read_timeout_normalized_to_urlerror():
    def fake_urlopen(request, timeout=None):
        raise TimeoutError("read timed out")

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(urllib.error.URLError):
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)


def test_http_error_propagates():
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)  # type: ignore[arg-type]

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(urllib.error.HTTPError):
            _fetcher().fetch_window(time_min=NOW, time_max=LATER)


# --- input guards + construction -----------------------------------------


def test_naive_window_raises():
    with pytest.raises(ValueError, match="timezone-aware"):
        _fetcher().fetch_window(time_min=datetime(2026, 7, 1, 8, 0), time_max=LATER)


def test_inverted_window_raises():
    with pytest.raises(ValueError, match="after time_min"):
        _fetcher().fetch_window(time_min=LATER, time_max=NOW)


def test_from_env_requires_key(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.setenv("COMPOSIO_USER_ID", SYNTH_USER)
    with pytest.raises(ValueError, match="COMPOSIO_API_KEY"):
        CalendarFetcher.from_env()


def test_from_env_requires_user(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", SYNTH_KEY)
    monkeypatch.delenv("COMPOSIO_USER_ID", raising=False)
    with pytest.raises(ValueError, match="COMPOSIO_USER_ID"):
        CalendarFetcher.from_env()


def test_from_env_honors_base_url_override(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", SYNTH_KEY)
    monkeypatch.setenv("COMPOSIO_USER_ID", SYNTH_USER)
    monkeypatch.setenv("COMPOSIO_BASE_URL", SYNTH_BASE)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _ok({"events": []})

    with patch("urllib.request.urlopen", fake_urlopen):
        CalendarFetcher.from_env().fetch_window(time_min=NOW, time_max=LATER)
    assert captured["url"].startswith(SYNTH_BASE)


# --- integration: fetched events are scan-compatible ---------------------


def test_fetched_events_feed_scan():
    with patch("urllib.request.urlopen", lambda r, timeout=None: _ok({"events": [_event("a")]})):
        events = _fetcher().fetch_window(time_min=NOW, time_max=LATER)
    [result] = scan(events, now=NOW, home_address="Home")
    assert result.meeting_id == "a"
    assert result.bucket == "needs_decision"
