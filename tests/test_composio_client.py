"""Tests for skills/flight-assist/composio_client.py.

Mocks `urllib.request.urlopen` so the tests exercise the client's request
shaping, the Composio success/failure envelope, status-code surfacing, and
transport-error normalization without touching the live Composio backend.
Synthetic fixtures only (no real API keys, no real calendar IDs).
"""

from __future__ import annotations

import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from composio_client import (  # noqa: E402
    ACTION_CREATE_EVENT,
    ACTION_DELETE_EVENT,
    ACTION_FIND_EVENTS,
    ACTION_LIST_CALENDARS,
    ComposioClient,
    ComposioError,
)

SYNTH_KEY = "synthetic_composio_key"
SYNTH_USER = "synthetic_user_42"
SYNTH_BASE = "https://composio.example/api/v3"


class _FakeResponse:
    """Stand-in for the urlopen context manager: yields .read()."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _ok(data: dict | None = None) -> _FakeResponse:
    body = json.dumps(
        {"data": data or {}, "successful": True, "error": None, "log_id": "log_ok"}
    ).encode()
    return _FakeResponse(body)


def _fail(
    error: str, *, status_code: int | None = None, message: str | None = None
) -> _FakeResponse:
    data: dict = {}
    if status_code is not None:
        data["status_code"] = status_code
    if message is not None:
        data["message"] = message
    body = json.dumps(
        {"data": data, "successful": False, "error": error, "log_id": "log_fail"}
    ).encode()
    return _FakeResponse(body)


@pytest.fixture
def client() -> ComposioClient:
    return ComposioClient(SYNTH_KEY, SYNTH_USER, base_url=SYNTH_BASE, timeout=5.0)


# --- construction / from_env ---------------------------------------------


def test_constructor_rejects_empty_api_key():
    with pytest.raises(ValueError, match="api_key is empty"):
        ComposioClient("", SYNTH_USER)


def test_constructor_rejects_empty_user_id():
    with pytest.raises(ValueError, match="user_id is empty"):
        ComposioClient(SYNTH_KEY, "")


def test_from_env_raises_when_api_key_unset(monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.setenv("COMPOSIO_USER_ID", SYNTH_USER)
    with pytest.raises(ValueError, match="COMPOSIO_API_KEY"):
        ComposioClient.from_env()


def test_from_env_raises_when_user_id_unset(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", SYNTH_KEY)
    monkeypatch.delenv("COMPOSIO_USER_ID", raising=False)
    with pytest.raises(ValueError, match="COMPOSIO_USER_ID"):
        ComposioClient.from_env()


def test_from_env_uses_default_base_url_when_override_unset(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", SYNTH_KEY)
    monkeypatch.setenv("COMPOSIO_USER_ID", SYNTH_USER)
    monkeypatch.delenv("COMPOSIO_BASE_URL", raising=False)
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok()

    c = ComposioClient.from_env()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        c.list_calendars()

    assert captured == [
        "https://backend.composio.dev/api/v3/tools/execute/" + ACTION_LIST_CALENDARS
    ]


def test_from_env_honors_base_url_override(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", SYNTH_KEY)
    monkeypatch.setenv("COMPOSIO_USER_ID", SYNTH_USER)
    monkeypatch.setenv("COMPOSIO_BASE_URL", SYNTH_BASE)
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok()

    c = ComposioClient.from_env()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        c.list_calendars()

    assert captured == [f"{SYNTH_BASE}/tools/execute/{ACTION_LIST_CALENDARS}"]


# --- request shaping ------------------------------------------------------


def test_execute_posts_action_url_with_auth_and_user_scoping(client):
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        return _ok({"items": []})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.execute("SOME_ACTION", {"calendar_id": "cal-1"})

    assert captured["url"] == f"{SYNTH_BASE}/tools/execute/SOME_ACTION"
    assert captured["method"] == "POST"
    assert captured["headers"]["x-api-key"] == SYNTH_KEY
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["body"] == {
        "user_id": SYNTH_USER,
        "arguments": {"calendar_id": "cal-1"},
    }


def test_named_methods_bind_their_action_slugs(client):
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.find_events({"calendar_id": "c"})
        client.create_event({"summary": "x"})
        client.delete_event({"event_id": "e"})

    assert captured == [
        f"{SYNTH_BASE}/tools/execute/{ACTION_FIND_EVENTS}",
        f"{SYNTH_BASE}/tools/execute/{ACTION_CREATE_EVENT}",
        f"{SYNTH_BASE}/tools/execute/{ACTION_DELETE_EVENT}",
    ]


# --- find_events pagination (#171) ---------------------------------------


def _find_page(events: list, token: str | None = None) -> _FakeResponse:
    """A FIND_EVENT page: double-nested events + optional nextPageToken."""
    inner: dict = {"event_data": events}
    if token is not None:
        inner["nextPageToken"] = token
    return _ok({"event_data": inner})


def test_find_events_single_page_when_no_token(client):
    """A window that fits one page costs exactly one call and returns its events."""
    calls = []

    def fake_urlopen(request, **kwargs):
        calls.append(json.loads(request.data)["arguments"])
        return _find_page([{"id": "e1"}, {"id": "e2"}])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.find_events({"calendar_id": "c"})

    assert len(calls) == 1
    assert result == {"event_data": {"event_data": [{"id": "e1"}, {"id": "e2"}]}}


def test_find_events_injects_max_results(client):
    """maxResults is set so the action does not silently cap at its ~10 default."""
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["args"] = json.loads(request.data)["arguments"]
        return _find_page([{"id": "e1"}])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.find_events({"calendar_id": "c"})

    assert captured["args"]["maxResults"] == 2500
    assert captured["args"]["calendar_id"] == "c"


def test_find_events_drains_all_pages_and_merges(client):
    """Multiple pages are followed via nextPageToken and merged into one shape.

    This is the storm fix (#171): a caller running `_items` over the return
    value sees every event in the window, not just the first page, so dedup
    can collapse the surplus instead of re-creating unseen blocks.
    """
    pages = [
        _find_page([{"id": "e1"}, {"id": "e2"}], token="tok-2"),
        _find_page([{"id": "e3"}, {"id": "e4"}], token="tok-3"),
        _find_page([{"id": "e5"}]),  # terminal page, no token
    ]
    sent_tokens = []

    def fake_urlopen(request, **kwargs):
        sent_tokens.append(json.loads(request.data)["arguments"].get("pageToken"))
        return pages[len(sent_tokens) - 1]

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.find_events({"calendar_id": "c"})

    # page 1 sends no token; pages 2 and 3 echo the prior page's nextPageToken
    assert sent_tokens == [None, "tok-2", "tok-3"]
    assert result["event_data"]["event_data"] == [
        {"id": "e1"},
        {"id": "e2"},
        {"id": "e3"},
        {"id": "e4"},
        {"id": "e5"},
    ]


def test_find_events_tolerates_flat_and_wrapped_page_shapes(client):
    """Page accumulation + token follow the same shapes callers' `_items` accept.

    The live toolkit double-nests, but Composio is mid-retirement; a flat
    `items` page (token at top level) and a `response_data` wrap must still
    drain, not silently return an empty merge or stop after page one.
    """
    pages = [
        _ok({"items": [{"id": "e1"}], "nextPageToken": "tok-2"}),  # flat shape
        _ok({"response_data": {"items": [{"id": "e2"}]}}),  # wrapped, terminal
    ]
    calls = []

    def fake_urlopen(request, **kwargs):
        calls.append(json.loads(request.data)["arguments"].get("pageToken"))
        return pages[len(calls) - 1]

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.find_events({"calendar_id": "c"})

    assert calls == [None, "tok-2"]
    assert result["event_data"]["event_data"] == [{"id": "e1"}, {"id": "e2"}]


def test_find_events_raises_when_token_never_clears(client):
    """A nextPageToken that never clears is bounded, not an infinite loop."""

    def fake_urlopen(request, **kwargs):
        return _find_page([{"id": "e"}], token="always-more")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(ComposioError, match="did not drain within"):
            client.find_events({"calendar_id": "c"})


def test_trailing_slash_in_base_url_does_not_double_up():
    c = ComposioClient(SYNTH_KEY, SYNTH_USER, base_url=SYNTH_BASE + "/")
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        c.list_calendars()

    assert captured == [f"{SYNTH_BASE}/tools/execute/{ACTION_LIST_CALENDARS}"]


# --- response envelope ----------------------------------------------------


def test_execute_returns_data_payload_on_success(client):
    payload = {"items": [{"id": "cal-1", "summary": "Flighty Flights"}]}
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _ok(payload)):
        result = client.list_calendars()
    assert result == payload


def test_success_with_null_data_returns_empty_dict(client):
    body = json.dumps({"data": None, "successful": True, "error": None}).encode()
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _FakeResponse(body)):
        result = client.execute("SOME_ACTION", {})
    assert result == {}


def test_tool_failure_raises_composio_error_with_status_code(client):
    """A 404 tool failure (event already gone) surfaces status_code=404 so
    the executor can treat the delete as an idempotent no-op."""
    response = _fail(
        "404 Client Error: Not Found",
        status_code=404,
        message="Event not found",
    )
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: response):
        with pytest.raises(ComposioError) as exc_info:
            client.delete_event({"calendar_id": "c", "event_id": "gone"})
    assert exc_info.value.status_code == 404
    assert "GOOGLECALENDAR_DELETE_EVENT failed" in str(exc_info.value)


def test_tool_failure_without_status_code_has_none(client):
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _fail("bad arguments")):
        with pytest.raises(ComposioError) as exc_info:
            client.create_event({})
    assert exc_info.value.status_code is None
    assert "bad arguments" in str(exc_info.value)


# --- transport errors -----------------------------------------------------


def test_http_error_propagates(client):
    """An HTTP-level failure (bad API key) is not a tool-envelope failure —
    it propagates as HTTPError without being wrapped in ComposioError."""

    def fake_urlopen(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", Message(), None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.list_calendars()
    assert exc_info.value.code == 401


def test_body_read_timeout_surfaces_as_urlerror(client):
    """A TimeoutError during response.read() must normalize to URLError so
    callers see one transport-error type. Mirrors byair_client (#28)."""

    class _ReadTimeoutResponse:
        def read(self):
            raise TimeoutError("body read exceeded socket timeout")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _ReadTimeoutResponse()):
        with pytest.raises(urllib.error.URLError) as exc_info:
            client.list_calendars()
    assert "timed out" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, TimeoutError)
