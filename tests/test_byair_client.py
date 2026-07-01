"""Tests for skills/flight-assist/byair_client.py.

Mocks `urllib.request.urlopen` so the tests exercise the client's parsing,
session-id handling, and error branching without touching the live byAir
endpoint. Synthetic fixtures only (no real flight numbers, no real API keys).
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

# E402 suppressed: the sys.path.insert above must execute before this import
# so the skill module resolves by bare name — its bundle dir is only on
# sys.path at runtime, matching nanoclaw-core's import convention.
from byair_client import ByAirClient, ByAirError  # noqa: E402

SYNTH_URL = "https://api.byairapp.example/mcp?api_key=synthetic_key"
SYNTH_SESSION = "TEST_SESSION_01HXYZ"


class _FakeResponse:
    """Stand-in for the urlopen context manager: yields .headers + .read()."""

    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body = body
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _initialize_response(session_id: str = SYNTH_SESSION) -> _FakeResponse:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "capabilities": {"tools": {"listChanged": True}},
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "byair-mcp-server", "version": "2.0.0"},
            },
        }
    ).encode()
    return _FakeResponse(body, {"mcp-session-id": session_id, "content-type": "application/json"})


def _initialized_notification_ack() -> _FakeResponse:
    # Notifications get no body; the client still calls .read() on the
    # response, so a zero-length body is fine.
    return _FakeResponse(b"", {"content-type": "application/json"})


def _tool_response(
    text_payload: dict | list | str,
    *,
    is_error: bool = False,
    error_type: str | None = None,
) -> _FakeResponse:
    text = json.dumps(text_payload) if isinstance(text_payload, (dict, list)) else text_payload
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
        result["_meta"] = {"error_type": error_type or "unknown"}
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "result": result}).encode()
    return _FakeResponse(body, {"content-type": "application/json"})


@pytest.fixture
def client() -> ByAirClient:
    return ByAirClient(SYNTH_URL, timeout=5.0)


def test_from_env_raises_when_url_unset(monkeypatch):
    monkeypatch.delenv("BYAIR_MCP_URL", raising=False)
    with pytest.raises(ValueError, match="BYAIR_MCP_URL"):
        ByAirClient.from_env()


def test_from_env_uses_url_from_env_var(monkeypatch):
    """A from_env-constructed client sends requests to the URL from BYAIR_MCP_URL."""
    monkeypatch.setenv("BYAIR_MCP_URL", SYNTH_URL)
    captured_urls = []

    def fake_urlopen(request, **kwargs):
        captured_urls.append(request.full_url)
        method = json.loads(request.data).get("method")
        if method == "initialize":
            return _initialize_response()
        if method == "notifications/initialized":
            return _initialized_notification_ack()
        return _tool_response({"id": 1})

    c = ByAirClient.from_env()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        c.get_flight(flight_id=1)

    assert all(url == SYNTH_URL for url in captured_urls)
    assert len(captured_urls) >= 3


def test_constructor_rejects_empty_url():
    with pytest.raises(ValueError, match="empty"):
        ByAirClient("")


def test_accept_header_advertises_both_json_and_event_stream(client):
    """byAir MCP streamable-HTTP spec rejects the request with HTTP 400 if
    the Accept header doesn't include BOTH 'application/json' AND
    'text/event-stream'. Regression test for the v0.1.x bug where the
    client sent only 'application/json' and every call failed at the
    handshake. Asserts the substring presence (the server uses substring
    matching, not parsed media-type lists)."""
    captured_accept_headers = []

    def fake_urlopen(request, **kwargs):
        captured_accept_headers.append(request.headers.get("Accept", ""))
        method = json.loads(request.data).get("method")
        if method == "initialize":
            return _initialize_response()
        if method == "notifications/initialized":
            return _initialized_notification_ack()
        return _tool_response({"id": 1})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.get_flight(flight_id=1)

    # Three requests: initialize + notifications/initialized + tools/call
    assert len(captured_accept_headers) == 3
    for accept in captured_accept_headers:
        assert "application/json" in accept, (
            f"Accept header must contain 'application/json' on every request. Got: {accept!r}"
        )
        assert "text/event-stream" in accept, (
            f"Accept header must contain 'text/event-stream' on every request "
            f"(byAir MCP spec requirement — server returns HTTP 400 otherwise). "
            f"Got: {accept!r}"
        )


def test_sse_response_raises_actionable_error(client):
    """The client advertises text/event-stream in Accept (spec requirement)
    but doesn't yet parse SSE response bodies. If the server picks SSE for
    a call we expected JSON for, raise a clear ByAirError instead of a
    cryptic json.JSONDecodeError. Verifies the Content-Type guard."""
    sse_response = _FakeResponse(
        b'event: message\ndata: {"chunk": 1}\n\n',
        {"content-type": "text/event-stream", "mcp-session-id": SYNTH_SESSION},
    )
    responses = iter([_initialize_response(), _initialized_notification_ack(), sse_response])
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(responses)):
        with pytest.raises(ByAirError) as exc_info:
            client.get_flight(flight_id=1)
    assert exc_info.value.error_type == "unsupported_response_shape"
    assert "SSE" in exc_info.value.message


def test_get_flight_success(client):
    payload = {"id": 999, "code": "XX123", "computed_status": "scheduled"}
    responses = iter(
        [
            _initialize_response(),
            _initialized_notification_ack(),
            _tool_response(payload),
        ]
    )
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(responses)):
        result = client.get_flight(flight_id=999)
    assert result == payload


def test_get_flight_not_found_raises_byair_error(client):
    responses = iter(
        [
            _initialize_response(),
            _initialized_notification_ack(),
            _tool_response("not_found: resource not found", is_error=True, error_type="not_found"),
        ]
    )
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(responses)):
        with pytest.raises(ByAirError) as exc_info:
            client.get_flight(flight_id=999999)
    assert exc_info.value.error_type == "not_found"


def test_list_trips_passes_arguments(client):
    payload = {"trips": []}
    captured_requests = []

    def fake_urlopen(request, **kwargs):
        captured_requests.append(json.loads(request.data))
        if len(captured_requests) == 1:
            return _initialize_response()
        if len(captured_requests) == 2:
            return _initialized_notification_ack()
        return _tool_response(payload)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.list_trips(status="expired", ownership="mine")

    assert result == payload
    tools_call_req = captured_requests[2]
    assert tools_call_req["method"] == "tools/call"
    assert tools_call_req["params"]["name"] == "byair_list_trips"
    assert tools_call_req["params"]["arguments"] == {
        "status": "expired",
        "ownership": "mine",
    }


def test_session_id_from_initialize_is_sent_on_subsequent_calls(client):
    """The session-id captured from initialize must appear on subsequent
    tools/call request headers (observed via the captured request, not via
    private client state)."""
    alt_session = "ALT_SESSION_xyz"
    captured_requests = []

    def fake_urlopen(request, **kwargs):
        captured_requests.append(
            {"data": json.loads(request.data), "headers": dict(request.headers)}
        )
        method = captured_requests[-1]["data"].get("method")
        if method == "initialize":
            return _initialize_response(session_id=alt_session)
        if method == "notifications/initialized":
            return _initialized_notification_ack()
        return _tool_response({"id": 1})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.get_flight(flight_id=1)

    notification = next(
        r for r in captured_requests if r["data"].get("method") == "notifications/initialized"
    )
    tool_call = next(r for r in captured_requests if r["data"].get("method") == "tools/call")
    # urllib lower-cases header names on the Request object; check the
    # capitalization-insensitive variants
    assert notification["headers"].get("Mcp-session-id") == alt_session
    assert tool_call["headers"].get("Mcp-session-id") == alt_session


def test_subsequent_calls_reuse_session(client):
    captured_requests = []

    def fake_urlopen(request, **kwargs):
        captured_requests.append(
            {"data": json.loads(request.data), "headers": dict(request.headers)}
        )
        if len(captured_requests) == 1:
            return _initialize_response()
        if len(captured_requests) == 2:
            return _initialized_notification_ack()
        return _tool_response({"id": captured_requests[-1]["data"].get("id", 0)})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.get_flight(flight_id=1)
        client.get_flight(flight_id=2)

    # Two get_flight calls, but only ONE initialize (call 1 + notification 2)
    assert len(captured_requests) == 4
    init_count = sum(1 for r in captured_requests if r["data"].get("method") == "initialize")
    assert init_count == 1
    # Both tool calls carry the session-id header
    tool_calls = [r for r in captured_requests if r["data"].get("method") == "tools/call"]
    assert len(tool_calls) == 2
    for call in tool_calls:
        assert call["headers"].get("Mcp-session-id") == SYNTH_SESSION


def test_session_expired_triggers_reinit_and_retry(client):
    payload = {"id": 1, "code": "OK"}
    call_count = {"n": 0}

    def fake_urlopen(request, **kwargs):
        call_count["n"] += 1
        method = json.loads(request.data).get("method")
        if method == "initialize":
            return _initialize_response()
        if method == "notifications/initialized":
            return _initialized_notification_ack()
        # tools/call: first invocation raises a 400 (session expired);
        # second invocation (after re-init) succeeds.
        if call_count["n"] == 3:
            raise urllib.error.HTTPError(
                SYNTH_URL, 400, "Bad Request", Message(), io.BytesIO(b"session expired")
            )
        return _tool_response(payload)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.get_flight(flight_id=1)

    assert result == payload
    # Sequence: init, notify, tool(400), init, notify, tool(200) — 6 calls
    assert call_count["n"] == 6


def test_session_expired_second_failure_propagates(client):
    def fake_urlopen(request, **kwargs):
        method = json.loads(request.data).get("method")
        if method == "initialize":
            return _initialize_response()
        if method == "notifications/initialized":
            return _initialized_notification_ack()
        raise urllib.error.HTTPError(
            SYNTH_URL, 400, "Bad Request", Message(), io.BytesIO(b"session expired")
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(urllib.error.HTTPError):
            client.get_flight(flight_id=1)


def test_non_session_http_error_propagates_immediately(client):
    """A 500 is not a session-expired signal — propagate without retry."""

    def fake_urlopen(request, **kwargs):
        method = json.loads(request.data).get("method")
        if method == "initialize":
            return _initialize_response()
        if method == "notifications/initialized":
            return _initialized_notification_ack()
        raise urllib.error.HTTPError(
            SYNTH_URL, 500, "Internal Server Error", Message(), io.BytesIO(b"oops")
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            client.get_flight(flight_id=1)
    assert exc_info.value.code == 500


def test_malformed_response_raises_byair_error(client):
    """A response with no text content block is structurally invalid."""

    def fake_urlopen(request, **kwargs):
        method = json.loads(request.data).get("method")
        if method == "initialize":
            return _initialize_response()
        if method == "notifications/initialized":
            return _initialized_notification_ack()
        body = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"content": []}}).encode()
        return _FakeResponse(body, {})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(ByAirError) as exc_info:
            client.get_flight(flight_id=1)
    assert exc_info.value.error_type == "malformed_response"


def test_body_read_timeout_surfaces_as_urlerror(client):
    """A TimeoutError during response.read() must surface as urllib.error.URLError.

    `urlopen(..., timeout=X)` wraps connect-side socket timeouts as URLError,
    but a timeout while reading the body propagates raw TimeoutError. The
    client must normalize so `_run_cycle`'s transient-transport branch
    catches every transport timeout uniformly. Regression for #28.
    """

    class _ReadTimeoutResponse:
        headers = {"content-type": "application/json"}

        def read(self):
            raise TimeoutError("body read exceeded socket timeout")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    responses = iter(
        [
            _initialize_response(),
            _initialized_notification_ack(),
            _ReadTimeoutResponse(),
        ]
    )
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(responses)):
        with pytest.raises(urllib.error.URLError) as exc_info:
            client.get_flight(flight_id=1)
    assert "timed out" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, TimeoutError)


def test_initialize_without_session_id_raises():
    """Server response missing the mcp-session-id header is unrecoverable."""
    client = ByAirClient(SYNTH_URL)
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}
    ).encode()
    bad_init = _FakeResponse(body, {"content-type": "application/json"})

    def fake_urlopen(request, **kwargs):
        return bad_init

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(ByAirError) as exc_info:
            client.get_flight(flight_id=1)
    assert exc_info.value.error_type == "session_missing"


# --- airport endpoints (real BNA-sampled fixtures) ---------------------

# Sampled from a live byair_get_airport(98) payload, trimmed to the fields
# the airport drive blocks consume plus the structured delay object.
BNA_AIRPORT = {
    "id": 98,
    "name": "Nashville International Airport",
    "code": "BNA",
    "cityName": "Nashville",
    "countryName": "United States",
    "countryFlag": "🇺🇸",
    "timezone": "America/Chicago",
    "lat": 36.125247,
    "lon": -86.677102,
    "delay": {
        "index": "low",
        "average": 17,
        "delayedPercentage": 15,
        "onTimePercentage": 85,
        "totalFlights": 384,
    },
    "delay_detail": "Minor delays possible",
}

# Sampled from a live byair_get_airport_tips(98) payload (a JSON array).
BNA_TIPS = [
    {
        "id": 6176,
        "category": "terminal",
        "aiGenerated": False,
        "author": {"name": "oil soaked rag", "isVerified": False},
        "rating": {"upvotes": 0, "downvotes": 1},
        "text": "It's a long walk to the rental car garage - keep that in mind!",
    },
]


def _airport_fake(payload, recorder):
    """urlopen stand-in: handshake once, then echo `payload` for each
    tools/call, recording (tool_name, airport_id) per call so tests can
    assert how many byAir calls the cache actually spent."""

    def fake_urlopen(request, **kwargs):
        data = json.loads(request.data)
        method = data.get("method")
        if method == "initialize":
            return _initialize_response()
        if method == "notifications/initialized":
            return _initialized_notification_ack()
        params = data["params"]
        recorder.append((params["name"], params["arguments"]["airport_id"]))
        return _tool_response(payload)

    return fake_urlopen


def test_get_airport_returns_payload(client):
    recorder = []
    with patch("urllib.request.urlopen", side_effect=_airport_fake(BNA_AIRPORT, recorder)):
        result = client.get_airport(airport_id=98)
    assert result == BNA_AIRPORT
    assert result["delay"]["index"] == "low"
    assert result["timezone"] == "America/Chicago"
    assert result["countryFlag"] == "🇺🇸"
    assert recorder == [("byair_get_airport", 98)]


def test_get_airport_tips_returns_list(client):
    recorder = []
    with patch("urllib.request.urlopen", side_effect=_airport_fake(BNA_TIPS, recorder)):
        result = client.get_airport_tips(airport_id=98)
    assert result == BNA_TIPS
    assert result[0]["category"] == "terminal"
    assert recorder == [("byair_get_airport_tips", 98)]


def test_get_airport_caches_same_id(client):
    recorder = []
    with patch("urllib.request.urlopen", side_effect=_airport_fake(BNA_AIRPORT, recorder)):
        first = client.get_airport(airport_id=98)
        second = client.get_airport(airport_id=98)
    # Second lookup served from cache: same object, only ONE byAir call spent.
    assert first is second
    assert recorder == [("byair_get_airport", 98)]


def test_get_airport_distinct_ids_each_fetch_once(client):
    recorder = []
    with patch("urllib.request.urlopen", side_effect=_airport_fake(BNA_AIRPORT, recorder)):
        client.get_airport(airport_id=98)
        client.get_airport(airport_id=98)
        client.get_airport(airport_id=1238)
    # Two distinct airports -> two calls; the repeat 98 is cached.
    assert recorder == [("byair_get_airport", 98), ("byair_get_airport", 1238)]


def test_get_airport_tips_cached_independently_of_airport(client):
    recorder = []
    with patch("urllib.request.urlopen", side_effect=_airport_fake(BNA_TIPS, recorder)):
        client.get_airport_tips(airport_id=98)
        client.get_airport_tips(airport_id=98)
    assert recorder == [("byair_get_airport_tips", 98)]


def test_get_airport_not_found_raises(client):
    responses = iter(
        [
            _initialize_response(),
            _initialized_notification_ack(),
            _tool_response("not_found: airport not found", is_error=True, error_type="not_found"),
        ]
    )
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(responses)):
        with pytest.raises(ByAirError) as exc_info:
            client.get_airport(airport_id=999999)
    assert exc_info.value.error_type == "not_found"
