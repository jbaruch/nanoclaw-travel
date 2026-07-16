"""Tests for skills/flight-assist/google_calendar_client.py.

Mocks `urllib.request.urlopen` so the tests exercise the client's request
shaping, the status-code surfacing callers gate idempotency on, the 401/403
config-error classification, and transport-error normalization without touching
the live Google Calendar API. Synthetic fixtures only (no real calendar IDs).

The credential assertions are the point of #638: there is no key to pass, no
`from_env`, and NO Authorization header may ever be sent — the OneCLI gateway
injects it on the wire.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from google_calendar_client import (  # noqa: E402
    GatewayNotInjecting,
    GoogleCalendarClient,
    GoogleCalendarError,
    TierAccessRestricted,
)

SYNTH_BASE = "https://calendar.example/calendar/v3"
SYNTH_CAL = "synthetic@group.calendar.example"


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


def _ok(payload: dict | None = None) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload if payload is not None else {}).encode())


def _http_error(code: int, reason: str, body: bytes = b"") -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(
        "https://calendar.example/x", code, reason, Message(), io.BytesIO(body)
    )


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


@pytest.fixture
def client() -> GoogleCalendarClient:
    return GoogleCalendarClient(base_url=SYNTH_BASE, timeout=5.0)


# --- construction ---------------------------------------------------------


def test_constructor_takes_no_credential_and_needs_no_env(monkeypatch):
    """#638: the container holds no Google credential. Construction must work
    with a completely bare environment — there is no key to be missing."""
    for var in ("COMPOSIO_API_KEY", "COMPOSIO_USER_ID", "GOOGLE_CALENDAR_API_BASE"):
        monkeypatch.delenv(var, raising=False)

    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok({"items": []})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        GoogleCalendarClient().list_calendars()

    assert captured == ["https://www.googleapis.com/calendar/v3/users/me/calendarList"]


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_API_BASE", SYNTH_BASE)
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        GoogleCalendarClient().list_calendars()

    assert captured == [f"{SYNTH_BASE}/users/me/calendarList"]


def test_explicit_base_url_beats_env_override(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_API_BASE", "https://ignored.example/v3")
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        GoogleCalendarClient(base_url=SYNTH_BASE).list_calendars()

    assert captured == [f"{SYNTH_BASE}/users/me/calendarList"]


def test_trailing_slash_in_base_url_does_not_double_up():
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        GoogleCalendarClient(base_url=SYNTH_BASE + "/").list_calendars()

    assert captured == [f"{SYNTH_BASE}/users/me/calendarList"]


# --- no credential on the wire --------------------------------------------


def test_no_authorization_header_is_ever_sent(client):
    """The gateway injects the Bearer on the wire. A client-sent auth header
    would mean a credential leaked into the container (#638)."""
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["headers"] = {k.lower() for k in request.headers}
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.create_event({"calendar_id": SYNTH_CAL, "summary": "x"})

    assert "authorization" not in captured["headers"]
    assert "x-api-key" not in captured["headers"]


# --- request shaping ------------------------------------------------------


def test_endpoints_and_verbs_per_method(client):
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append((request.get_method(), urllib.parse.urlsplit(request.full_url).path))
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.list_calendars()
        client.find_events({"calendar_id": "c"})
        client.create_event({"calendar_id": "c", "summary": "x"})
        client.patch_event({"calendar_id": "c", "event_id": "e", "summary": "y"})
        client.delete_event({"calendar_id": "c", "event_id": "e"})

    assert captured == [
        ("GET", "/calendar/v3/users/me/calendarList"),
        ("GET", "/calendar/v3/calendars/c/events"),
        ("POST", "/calendar/v3/calendars/c/events"),
        ("PATCH", "/calendar/v3/calendars/c/events/e"),
        ("DELETE", "/calendar/v3/calendars/c/events/e"),
    ]


def test_calendar_id_is_url_quoted_onto_the_path(client):
    """Calendar ids are email addresses — the `@` must reach Calendar encoded,
    not read as URL syntax."""
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request.full_url)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.find_events({"calendar_id": SYNTH_CAL})

    assert captured[0].startswith(
        f"{SYNTH_BASE}/calendars/synthetic%40group.calendar.example/events?"
    )


def test_event_id_is_url_quoted_onto_the_path(client):
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(urllib.parse.urlsplit(request.full_url).path)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.delete_event({"calendar_id": "c", "event_id": "ev/1+2"})

    assert captured == ["/calendar/v3/calendars/c/events/ev%2F1%2B2"]


def test_routing_keys_are_stripped_from_the_body(client):
    """`calendar_id` / `event_id` are this client's own routing keys — they go
    on the path, never into the event resource Google stores."""
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["body"] = json.loads(request.data)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.patch_event(
            {"calendar_id": "c", "event_id": "e", "summary": "Drive: x", "location": "Home"}
        )

    assert captured["body"] == {"summary": "Drive: x", "location": "Home"}


def test_create_body_passes_native_fields_through_verbatim(client):
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["body"] = json.loads(request.data)
        captured["content_type"] = request.headers.get("Content-type")
        return _ok()

    body = {
        "summary": "Drive: Customer sync",
        "description": "state",
        "location": "Office",
        "start": {"dateTime": "2026-07-01T13:30:00-05:00", "timeZone": "America/Chicago"},
        "end": {"dateTime": "2026-07-01T14:00:00-05:00", "timeZone": "America/Chicago"},
        "transparency": "transparent",
    }
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.create_event({"calendar_id": "c", **body})

    assert captured["body"] == body
    assert captured["content_type"] == "application/json"


def test_caller_arguments_are_not_mutated(client):
    """The client pops its routing keys off a COPY — a caller that reuses its
    args dict (the create loop retries idempotently) must not find it gutted."""
    args = {"calendar_id": "c", "event_id": "e", "summary": "x"}
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _ok()):
        client.patch_event(args)
    assert args == {"calendar_id": "c", "event_id": "e", "summary": "x"}


def test_get_sends_no_body(client):
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["data"] = request.data
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.find_events({"calendar_id": "c"})

    assert captured["data"] is None


# --- query encoding -------------------------------------------------------


def test_booleans_serialize_as_json_not_python(client):
    """A real Python `True` must reach Google as `true`. urlencode would render
    it `True`, which Google rejects as a malformed value.

    Asserted with an actual bool, not the string "true" — a string fixture
    would pass whether or not the serializer exists, masking the bug.
    """
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(_query(request.full_url))
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.find_events({"calendar_id": "c", "singleEvents": True, "showDeleted": False})

    assert captured[0]["singleEvents"] == "true"
    assert captured[0]["showDeleted"] == "false"


def test_query_params_are_passed_through_natively(client):
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(_query(request.full_url))
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.find_events(
            {
                "calendar_id": "c",
                "timeMin": "2026-07-01T00:00:00Z",
                "timeMax": "2026-07-02T00:00:00Z",
                "orderBy": "startTime",
                "singleEvents": True,
            }
        )

    assert captured[0]["timeMin"] == "2026-07-01T00:00:00Z"
    assert captured[0]["timeMax"] == "2026-07-02T00:00:00Z"
    assert captured[0]["orderBy"] == "startTime"


def test_list_params_repeat_the_key(client):
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(urllib.parse.urlsplit(request.full_url).query)
        return _ok()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.list_calendars({"fields": ["id", "summary"]})

    assert captured[0] == "fields=id&fields=summary"


# --- find_events pagination (#171) ---------------------------------------


def _page(events: list, token: str | None = None) -> _FakeResponse:
    payload: dict = {"items": events}
    if token is not None:
        payload["nextPageToken"] = token
    return _ok(payload)


def test_find_events_single_page_when_no_token(client):
    """A window that fits one page costs exactly one call and returns its events."""
    calls = []

    def fake_urlopen(request, **kwargs):
        calls.append(request.full_url)
        return _page([{"id": "e1"}, {"id": "e2"}])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.find_events({"calendar_id": "c"})

    assert len(calls) == 1
    assert result == {"items": [{"id": "e1"}, {"id": "e2"}]}


def test_find_events_injects_max_results(client):
    """maxResults is set so events.list does not silently cap at its 250 default."""
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(_query(request.full_url))
        return _page([{"id": "e1"}])

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.find_events({"calendar_id": "c"})

    assert captured[0]["maxResults"] == "2500"


def test_find_events_drains_all_pages_and_merges(client):
    """Multiple pages are followed via nextPageToken and merged into one shape.

    This is the storm fix (#171): a caller reading `items` off the return value
    sees every event in the window, not just the first page, so dedup can
    collapse the surplus instead of re-creating unseen blocks.
    """
    pages = [
        _page([{"id": "e1"}, {"id": "e2"}], token="tok-2"),
        _page([{"id": "e3"}, {"id": "e4"}], token="tok-3"),
        _page([{"id": "e5"}]),  # terminal page, no token
    ]
    sent_tokens = []

    def fake_urlopen(request, **kwargs):
        sent_tokens.append(_query(request.full_url).get("pageToken"))
        return pages[len(sent_tokens) - 1]

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.find_events({"calendar_id": "c"})

    # page 1 sends no token; pages 2 and 3 echo the prior page's nextPageToken
    assert sent_tokens == [None, "tok-2", "tok-3"]
    assert result["items"] == [
        {"id": "e1"},
        {"id": "e2"},
        {"id": "e3"},
        {"id": "e4"},
        {"id": "e5"},
    ]


def test_find_events_merged_shape_matches_single_page_shape(client):
    """A merged multi-page result and a one-page result are the same shape, so
    a caller's extraction never has to know how many pages it took."""
    pages = [_page([{"id": "e1"}], token="t"), _page([{"id": "e2"}])]
    calls = []

    def fake_urlopen(request, **kwargs):
        calls.append(1)
        return pages[len(calls) - 1]

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.find_events({"calendar_id": "c"})

    assert set(result) == {"items"}


def test_find_events_page_without_items_is_a_terminal_no_op(client):
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _ok({})):
        assert client.find_events({"calendar_id": "c"}) == {"items": []}


def test_find_events_raises_when_token_never_clears(client):
    """A nextPageToken that never clears is bounded, not an infinite loop."""

    def fake_urlopen(request, **kwargs):
        return _page([{"id": "e"}], token="always-more")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(GoogleCalendarError, match="did not drain within"):
            client.find_events({"calendar_id": "c"})


# --- responses ------------------------------------------------------------


def test_delete_204_empty_body_returns_empty_dict(client):
    """Calendar answers DELETE with 204 and no body. That is success, not a
    JSONDecodeError for every caller to guard."""
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _FakeResponse(b"")):
        assert client.delete_event({"calendar_id": "c", "event_id": "e"}) == {}


def test_create_returns_the_event_resource(client):
    payload = {"id": "evt_new", "summary": "Drive: x"}
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _ok(payload)):
        assert client.create_event({"calendar_id": "c", "summary": "x"}) == payload


# --- errors ---------------------------------------------------------------


def test_404_surfaces_status_code_for_idempotent_delete(client):
    """The load-bearing contract: callers treat a 404 on delete as "already
    gone = success". Composio faked this status inside an HTTP-200 envelope;
    now it is the real HTTP status, and `.status_code` must still carry it."""

    def fake_urlopen(request, **kwargs):
        raise _http_error(404, "Not Found", b'{"error": {"message": "Not Found"}}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(GoogleCalendarError) as exc_info:
            client.delete_event({"calendar_id": "c", "event_id": "gone"})

    assert exc_info.value.status_code == 404


def test_error_message_preserves_googles_reason(client):
    """Classifying reads the error body, which DRAINS it. The reason Google
    sent must survive into the message rather than being lost to the read."""

    def fake_urlopen(request, **kwargs):
        raise _http_error(403, "Forbidden", b'{"error": {"errors": [{"reason": "rateLimit')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(GoogleCalendarError) as exc_info:
            client.create_event({"calendar_id": "c"})

    assert exc_info.value.status_code == 403
    assert "rateLimit" in str(exc_info.value)


def test_500_surfaces_status_code(client):
    def fake_urlopen(request, **kwargs):
        raise _http_error(500, "Internal Server Error")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(GoogleCalendarError) as exc_info:
            client.list_calendars()

    assert exc_info.value.status_code == 500


def test_401_raises_gateway_not_injecting(client):
    """401 means no Bearer reached Google: the gateway is off our request path
    or the app is disconnected. Actionable, and not retryable."""

    def fake_urlopen(request, **kwargs):
        raise _http_error(401, "Unauthorized", b'{"error": "invalid_credentials"}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(GatewayNotInjecting) as exc_info:
            client.list_calendars()

    assert "HTTPS_PROXY" in str(exc_info.value)


def test_gateway_not_injecting_is_not_caught_as_a_per_op_failure(client):
    """A config error must not be swallowed by the per-op handlers that collect
    GoogleCalendarError and defer to the next cycle — it would defer forever."""

    def fake_urlopen(request, **kwargs):
        raise _http_error(401, "Unauthorized")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(GatewayNotInjecting):
            try:
                client.list_calendars()
            except GoogleCalendarError:  # pragma: no cover - must not match
                pytest.fail("GatewayNotInjecting must not be a GoogleCalendarError")


def test_403_access_restricted_raises_tier_access_restricted(client):
    """The untrusted tier is gated from Google by design (#638) — correct
    behaviour, reported distinctly from a broken gateway."""

    def fake_urlopen(request, **kwargs):
        raise _http_error(403, "Forbidden", b'{"error": "access_restricted"}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(TierAccessRestricted):
            client.find_events({"calendar_id": "c"})


def test_tier_access_restricted_is_not_caught_as_a_per_op_failure(client):
    def fake_urlopen(request, **kwargs):
        raise _http_error(403, "Forbidden", b'{"error": "access_restricted"}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(TierAccessRestricted):
            try:
                client.find_events({"calendar_id": "c"})
            except GoogleCalendarError:  # pragma: no cover - must not match
                pytest.fail("TierAccessRestricted must not be a GoogleCalendarError")


def test_plain_403_is_not_mistaken_for_a_tier_restriction(client):
    """An ordinary 403 (rate limit, missing scope) is a per-op failure with a
    status code — only the `access_restricted` marker means the tier gate."""

    def fake_urlopen(request, **kwargs):
        raise _http_error(403, "Forbidden", b'{"error": {"reason": "insufficientPermissions"}}')

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(GoogleCalendarError) as exc_info:
            client.find_events({"calendar_id": "c"})

    assert exc_info.value.status_code == 403


def test_unreadable_error_body_still_classifies(client):
    """Reading the body can itself fail; that must not mask the status."""

    def _raise_on_read(*args, **kwargs):
        raise OSError("connection reset while reading error body")

    def fake_urlopen(request, **kwargs):
        err = _http_error(404, "Not Found")
        # setattr, not `err.read = ...`: HTTPError declares `read(n=...)`, so a
        # plain assignment is an incompatible-signature error under pyright.
        setattr(err, "read", _raise_on_read)  # noqa: B010
        raise err

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(GoogleCalendarError) as exc_info:
            client.delete_event({"calendar_id": "c", "event_id": "e"})

    assert exc_info.value.status_code == 404


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


def test_network_failure_propagates_as_urlerror(client):
    def fake_urlopen(request, **kwargs):
        raise urllib.error.URLError("name resolution failed")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(urllib.error.URLError):
            client.list_calendars()
