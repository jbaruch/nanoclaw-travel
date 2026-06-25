"""HTTP client for the byAir MCP streamable-HTTP endpoint, called as an API.

The endpoint speaks JSON-RPC 2.0 over HTTP with session-id continuity. Used
by the precheck script for polling-time queries; not registered as a Claude
MCP tool inside the agent container (the agent never sees the raw 13KB
responses; the precheck filters to the ~1KB operational slice before any
state write).

stdlib-only: `urllib.request` + `json` per `jbaruch/coding-policy:
dependency-management` (Stdlib First).

Public API:
    # The skill bundle dir is added to sys.path at invocation time; this
    # module is imported by its bare name (matches nanoclaw-core's convention).
    from byair_client import ByAirClient, ByAirError

    client = ByAirClient.from_env()
    flight = client.get_flight(flight_id=12345)
    trips = client.list_trips(status="active")

Errors:
    - `ByAirError` wraps `isError: true` responses; `error_type` exposes
      the byAir `_meta.error_type` ("not_found", etc.)
    - HTTP / network errors propagate as `urllib.error.URLError` /
      `urllib.error.HTTPError` per `jbaruch/coding-policy: error-handling`
      "Specific Exceptions"
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "nanoclaw-flight-assist", "version": "0.1.0"}


class ByAirError(Exception):
    """Raised when the byAir endpoint returns `isError: true` on a tool call."""

    def __init__(self, error_type: str, message: str):
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


class ByAirClient:
    """Thin JSON-RPC client for the byAir MCP endpoint.

    Sessions are initialized lazily on the first request and reused for
    subsequent calls. On session-invalid responses (HTTP 400 / 404 with
    the session-id header rejected), the client transparently re-initializes
    once and retries the call; a second failure propagates.

    Not thread-safe — one client per process is the intended shape.
    """

    def __init__(self, url: str, *, timeout: float = 30.0):
        if not url:
            raise ValueError(
                "ByAirClient: url is empty — set BYAIR_MCP_URL in the env "
                "(personal MCP link from https://byairapp.com/mcp/) or pass it explicitly"
            )
        self._url = url
        self._timeout = timeout
        self._session_id: str | None = None
        self._next_id = 0

    @classmethod
    def from_env(cls, *, env_var: str = "BYAIR_MCP_URL", timeout: float = 30.0) -> ByAirClient:
        """Construct from the BYAIR_MCP_URL env var."""
        url = os.environ.get(env_var, "")
        if not url:
            raise ValueError(
                f"ByAirClient.from_env: ${env_var} is unset — add the personal MCP link from "
                f"https://byairapp.com/mcp/ to OneCLI vault and restart the container"
            )
        return cls(url, timeout=timeout)

    def get_flight(self, flight_id: int) -> dict:
        """Get flight details by ID. Returns the byAir flight payload as a dict.

        Raises ByAirError("not_found", ...) when the flight_id is unknown.
        """
        return self._call_tool("byair_get_flight", {"flight_id": flight_id})

    def list_trips(self, status: str = "active", ownership: str = "all") -> dict:
        """List the user's tracked trips. Returns the byAir list-trips payload.

        Args:
            status: "active" or "expired"
            ownership: "mine", "friend", or "all"
        """
        return self._call_tool("byair_list_trips", {"status": status, "ownership": ownership})

    def get_flight_notifications(self, flight_id: int) -> dict:
        """Get push-notification settings for a tracked flight."""
        return self._call_tool("byair_get_flight_notifications", {"flight_id": flight_id})

    def _call_tool(self, name: str, arguments: dict) -> dict:
        """Send a tools/call request, returning the decoded inner payload."""
        if self._session_id is None:
            self._initialize()
        try:
            return self._tools_call(name, arguments)
        except _SessionExpired:
            # One transparent re-init + retry per `coding-policy: error-handling`
            # "Graceful Fallback". A second failure surfaces the underlying
            # HTTPError from the SECOND attempt, so diagnostics reflect the
            # final transport failure rather than a stale prior error.
            self._session_id = None
            self._initialize()
            try:
                return self._tools_call(name, arguments)
            except _SessionExpired as second_failure:
                # `_SessionExpired` is always raised `from` the underlying
                # transport error, so `__cause__` carries the real HTTPError to
                # surface. Guard the None case anyway — `raise None` would be a
                # TypeError that buries the actual failure.
                cause = second_failure.__cause__
                if cause is None:
                    raise
                raise cause from None

    def _initialize(self) -> None:
        """Run the MCP initialize handshake; capture the session-id header."""
        payload = self._rpc_envelope(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
        }
        response, response_headers = self._http_post(headers, payload)
        session_id = response_headers.get("mcp-session-id")
        if not session_id:
            raise ByAirError(
                "session_missing",
                "initialize response did not include mcp-session-id header",
            )
        self._session_id = session_id
        _ = response  # body parsed but not retained — server caps are not needed for the API path

        # Notify the server we're initialized (no response expected)
        notify_payload = self._rpc_envelope("notifications/initialized", None, notification=True)
        notify_headers = {**headers, "Mcp-Session-Id": self._session_id}
        self._http_post(notify_headers, notify_payload, expect_response=False)

    def _tools_call(self, name: str, arguments: dict) -> dict:
        """Send tools/call and decode the double-encoded response."""
        assert self._session_id is not None
        payload = self._rpc_envelope("tools/call", {"name": name, "arguments": arguments})
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
            "Mcp-Session-Id": self._session_id,
        }
        body, _ = self._http_post(headers, payload)
        result = body.get("result", {})
        is_error = result.get("isError", False)
        content_list = result.get("content", [])
        if not content_list or content_list[0].get("type") != "text":
            raise ByAirError(
                "malformed_response",
                f"{name} response had no text content block — got {result!r}",
            )
        text_payload = content_list[0]["text"]
        if is_error:
            error_type = result.get("_meta", {}).get("error_type", "unknown")
            raise ByAirError(error_type, text_payload)
        return json.loads(text_payload)

    def _http_post(
        self, headers: dict, payload: bytes, *, expect_response: bool = True
    ) -> tuple[dict, dict]:
        """POST a JSON-RPC payload; return (parsed_body, lowercased_headers).

        On HTTP 4xx with a session-id-rejected status (400/404 after a session
        was issued), raises `_SessionExpired` so the caller can re-init and retry.

        Content-Type guard: the byAir MCP server requires the `Accept` header
        to advertise both `application/json` and `text/event-stream` (servers
        MAY stream tool responses via SSE per the MCP streamable-HTTP spec).
        For the operations this client uses (initialize, notifications, and
        non-streaming tool calls — byair_get_flight, byair_list_trips,
        byair_get_flight_notifications), the server returns JSON. We don't
        parse SSE here. The Content-Type guard raises a clear actionable
        error if the server picks SSE for one of our calls, rather than
        letting `json.loads` fail with a cryptic decoder error on an
        `event:` / `data:` prefix.
        """
        request = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                if not expect_response:
                    return ({}, response_headers)
                content_type = response_headers.get("content-type", "")
                if content_type.startswith("text/event-stream"):
                    raise ByAirError(
                        "unsupported_response_shape",
                        f"byAir returned SSE response (Content-Type: {content_type}) on a "
                        f"non-streaming call. This client does not yet parse SSE. The byAir "
                        f"server should return JSON for byair_get_flight / byair_list_trips / "
                        f"byair_get_flight_notifications; if SSE is returned for one of these, "
                        f"the server's streaming behaviour changed and the client needs an "
                        f"SSE-parsing path added in `_http_post`.",
                    )
                raw = response.read().decode("utf-8")
                return (json.loads(raw), response_headers)
        except urllib.error.HTTPError as http_err:
            # Session expired: 400 or 404 with a prior session-id sent.
            if http_err.code in (400, 404) and self._session_id is not None:
                raise _SessionExpired() from http_err
            raise
        except TimeoutError as timeout_err:
            # `urlopen` wraps connect-side socket timeouts as URLError, but a
            # timeout during `response.read()` of the body surfaces as raw
            # TimeoutError (socket.timeout is aliased to TimeoutError since
            # Python 3.10). Normalize so callers see a single transport-error
            # type per this module's docstring contract. Per #28.
            raise urllib.error.URLError(f"timed out: {timeout_err}") from timeout_err

    def _rpc_envelope(
        self, method: str, params: dict | None, *, notification: bool = False
    ) -> bytes:
        """Build a JSON-RPC 2.0 envelope; notifications omit the id field."""
        envelope: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            envelope["params"] = params
        if not notification:
            self._next_id += 1
            envelope["id"] = self._next_id
        return json.dumps(envelope).encode("utf-8")


class _SessionExpired(Exception):
    """Internal signal: session-id was rejected; caller should re-init + retry once."""
