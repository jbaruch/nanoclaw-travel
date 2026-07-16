"""HTTP client for the native Google Calendar REST API (v3).

Replaces `composio_client.py` (nanoclaw#638). Used by the calendar
`reconcile` script, the drive-engine sweep, and the drive-planner apply /
fetch paths to converge calendars deterministically — list calendars, find
events in a time window, create / patch / delete events — without the agent
in the loop. Mirrors `byair_client.py` / `maps_client.py`: stdlib-only
`urllib`, HTTP-mockable in CI, one client per process.

Credential model (the whole point of #638)
------------------------------------------
This container holds NO Google credential. OneCLI's TLS-MITM gateway owns the
OAuth connection: it injects `Authorization: Bearer` on the wire to the Google
API host and auto-refreshes the token. So construction takes no credential,
there is no `from_env`, and this client sends NO auth header — a request
carrying its own Authorization header would mean a credential leaked into the
container, not a fallback. `COMPOSIO_API_KEY` / `COMPOSIO_USER_ID` are gone.

The gateway reaches this process via `HTTPS_PROXY` + the mounted CA bundle,
both set on the spawn by the orchestrator. Nothing here configures the proxy;
`urllib` honours `HTTPS_PROXY` from the environment.

Errors, and why `.status_code` survives the migration
-----------------------------------------------------
Composio reported an API-level failure as HTTP 200 with `successful: false`
and faked the upstream status in `data.status_code`. Google reports the same
failures as REAL HTTP status codes. The envelope dies; `.status_code` does
NOT — callers gate idempotency on it (a delete that 404s means the event is
already gone, which is success), so it is still populated, now from the
actual HTTP status:

    GoogleCalendarError  — a non-2xx from Calendar; `.status_code` is the HTTP
        status (404 on a missing event, 403 rateLimitExceeded, ...). This is
        the per-op failure type callers catch and collect.
    GatewayNotInjecting  — 401. NOT a GoogleCalendarError: the gateway is off
        this process's request path, or the Google app is disconnected. No
        retry fixes either, and a per-op handler that collected it would
        silently defer every op forever, so it propagates past them to the
        process boundary where the operator gets told what to fix.
    TierAccessRestricted — 403 + `access_restricted`. Also not a
        GoogleCalendarError: the agent's OneCLI secretMode is `selective` and
        the untrusted tier is gated from Google by design (#638). Correct
        behaviour, not a fault.

Transport failures (network, body-read timeout) propagate as
`urllib.error.URLError` per `jbaruch/coding-policy: error-handling`
("Specific Exceptions").

stdlib-only: `urllib.request` + `json` per `jbaruch/coding-policy:
dependency-management` (Stdlib First).

Public API:
    # The skill bundle dir is added to sys.path at invocation time; this
    # module is imported by its bare name (matches nanoclaw-core's convention).
    from google_calendar_client import GoogleCalendarClient, GoogleCalendarError

    client = GoogleCalendarClient()
    calendars = client.list_calendars()               # -> {"items": [...]}
    events = client.find_events({"calendar_id": cid,  # -> {"items": [...]}
                                 "timeMin": lo, "timeMax": hi})
    client.delete_event({"calendar_id": cid, "event_id": eid})

Argument contract: `calendar_id` / `event_id` are THIS client's own keys —
routing, not payload. They are popped and URL-quoted onto the path (calendar
ids are email addresses, so the `@` must survive). Every OTHER key is native
Google Calendar: query params on `find_events`, and the event-resource body on
`create_event` / `patch_event` (`summary`, `location`, `description`,
`transparency`, and nested `start` / `end` objects). Nothing is translated —
what a caller passes is what Google receives.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# The Google Calendar v3 base. This is the exact host OneCLI's app connection
# is bound to — changing it without adding the matching OneCLI app binding
# means the gateway won't inject a Bearer and every call 401s.
_DEFAULT_BASE_URL = "https://www.googleapis.com/calendar/v3"

# Test-only override, mirroring nanoclaw-admin's GOOGLE_API_BASES. Tests point
# the client at a local fixture server; unset in production. Named for this
# client because this bundle talks to one surface only.
_BASE_URL_VAR = "GOOGLE_CALENDAR_API_BASE"

# events.list pagination. Google caps a page at 2500 events; left to its
# default the endpoint returns 250 and a caller reconciles against a partial
# window (#171: truncated current_blocks defeats dedup -> duplicate storm).
# 2500 drains any realistic window in a single call; the nextPageToken loop is
# the safety net for a window that still exceeds one page. _MAX_PAGES bounds
# the loop so a token that never clears can't spin forever.
_FIND_EVENT_PAGE_SIZE = 2500
_FIND_EVENT_MAX_PAGES = 40

GATEWAY_NOT_INJECTING_HINT = (
    "the OneCLI gateway is not authenticating this request. Check that the spawn "
    "carries HTTPS_PROXY + the mounted CA (src/container-runner.ts), and that the "
    "Google app is still connected in the vault (`onecli apps list` on the NAS)"
)

TIER_ACCESS_RESTRICTED_HINT = (
    "this agent's OneCLI secretMode is 'selective' — the untrusted tier is gated "
    "from Google by design (nanoclaw#638). Calendar writes do not run at this tier"
)


class GoogleCalendarError(Exception):
    """Raised when Google Calendar answers a call with a non-2xx status.

    `status_code` is that HTTP status (e.g. 404 from a missing event), else
    None for a failure with no status to report. Callers gate idempotency on
    it — a delete that 404s means the event is already gone, which is success.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class GatewayNotInjecting(RuntimeError):
    """Google answered 401, so no Bearer reached it.

    Deliberately NOT a GoogleCalendarError: callers catch that per-op and
    collect the failure for the next cycle, which for a config error would
    mean deferring every op forever with nothing actionable surfaced. This
    propagates to the process boundary, which prints
    GATEWAY_NOT_INJECTING_HINT and exits non-zero.
    """


class TierAccessRestricted(RuntimeError):
    """The OneCLI gateway refused to inject for this agent's tier.

    Expected on the untrusted tier (secretMode=selective). Callers report
    "unavailable at this tier" rather than treating it as a fault.
    """


class GoogleCalendarClient:
    """Thin REST client for the native Google Calendar v3 API.

    Takes no credential: the OneCLI gateway injects the Bearer on the wire
    (see the module docstring). Not thread-safe — one client per process is
    the intended shape, matching `byair_client.ByAirClient`.
    """

    def __init__(self, *, base_url: str | None = None, timeout: float = 30.0):
        # Read the override per-construction rather than at import so a test
        # can set it after this module is loaded.
        resolved = base_url or os.environ.get(_BASE_URL_VAR) or _DEFAULT_BASE_URL
        self._base_url = resolved.rstrip("/")
        self._timeout = timeout

    # --- Calendar surface -------------------------------------------------

    def list_calendars(self, arguments: dict | None = None) -> dict:
        """List the operator's calendars (`GET /users/me/calendarList`).

        Returns the native calendarList resource; the calendars are in `items`.
        """
        return self._request("GET", "/users/me/calendarList", params=arguments or {})

    def find_events(self, arguments: dict) -> dict:
        """Find events by calendar + time window, draining the COMPLETE window.

        `arguments` carries this client's `calendar_id` plus native
        events.list query params (`timeMin`, `timeMax`, `singleEvents`, ...).
        Note `orderBy: "startTime"` is only legal alongside `singleEvents:
        true` — Google rejects it otherwise.

        Sets `maxResults` and follows `nextPageToken` until the window is
        exhausted, then returns a single `{"items": [...]}` resource holding
        the merged events — the same shape a one-page response has, so a
        caller's event extraction is unchanged whether the window took one
        page or ten (#171). Without this, callers reconcile against a
        truncated first page and re-create everything they can't see.

        Raises:
            GoogleCalendarError: on a non-2xx (per `_request`), or if the
                window needs more than `_FIND_EVENT_MAX_PAGES` pages to drain
                (an implausibly large window, or a `nextPageToken` that never
                clears).
        """
        args = dict(arguments)
        calendar_id = args.pop("calendar_id")
        path = f"/calendars/{_quote(calendar_id)}/events"
        merged: list = []
        params = {**args, "maxResults": _FIND_EVENT_PAGE_SIZE}
        for _ in range(_FIND_EVENT_MAX_PAGES):
            page = self._request("GET", path, params=params)
            items = page.get("items")
            merged.extend(items if isinstance(items, list) else [])
            token = page.get("nextPageToken")
            if not isinstance(token, str) or not token:
                return {"items": merged}
            params = {**params, "pageToken": token}
        raise GoogleCalendarError(
            f"find_events: window did not drain within {_FIND_EVENT_MAX_PAGES} pages "
            f"(>{_FIND_EVENT_MAX_PAGES * _FIND_EVENT_PAGE_SIZE} events) — the time window "
            f"is implausibly large or nextPageToken is not clearing; narrow the window"
        )

    def create_event(self, arguments: dict) -> dict:
        """Create a calendar event (`POST /calendars/{calendarId}/events`).

        `arguments` carries this client's `calendar_id`; everything else is
        the native event-resource body. Returns the created event resource,
        whose new id is top-level `id`.
        """
        body = dict(arguments)
        calendar_id = body.pop("calendar_id")
        return self._request("POST", f"/calendars/{_quote(calendar_id)}/events", body=body)

    def patch_event(self, arguments: dict) -> dict:
        """Partial-update an event (`PATCH /calendars/{calendarId}/events/{eventId}`).

        Only the fields present in `arguments` are sent, so a patch never
        clobbers a field it did not touch — that is what makes PATCH the right
        verb here rather than the whole-resource `update` (PUT).
        """
        body = dict(arguments)
        calendar_id = body.pop("calendar_id")
        event_id = body.pop("event_id")
        path = f"/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}"
        return self._request("PATCH", path, body=body)

    def delete_event(self, arguments: dict) -> dict:
        """Delete an event (`DELETE /calendars/{calendarId}/events/{eventId}`).

        Google answers 204 with an empty body; that surfaces here as `{}` (see
        `_request`). A missing event raises `GoogleCalendarError(status_code=404)`;
        callers treat that as an idempotent success (event already gone).
        """
        args = dict(arguments)
        calendar_id = args.pop("calendar_id")
        event_id = args.pop("event_id")
        path = f"/calendars/{_quote(calendar_id)}/events/{_quote(event_id)}"
        return self._request("DELETE", path)

    # --- transport --------------------------------------------------------

    def _request(self, method: str, path: str, *, params: dict | None = None, body=None) -> dict:
        """Issue one native Calendar call; return the parsed JSON object.

        Raises:
            GatewayNotInjecting: on 401 (see the module docstring).
            TierAccessRestricted: on 403 + `access_restricted`.
            GoogleCalendarError: on any other non-2xx; `.status_code` is the
                HTTP status.
            urllib.error.URLError: on network/transport failure (incl. a
                body-read timeout, normalized for a single transport-error
                type per this module's contract).
        """
        url = f"{self._base_url}{path}"
        if params:
            # `doseq` keeps a list arg as repeated keys rather than
            # url-encoding the Python list repr — Google's list endpoints take
            # repeated keys for multi-value params.
            query = urllib.parse.urlencode(
                {k: _query_value(v) for k, v in params.items()}, doseq=True
            )
            url = f"{url}?{query}"

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        # No Authorization header by design — the OneCLI gateway injects it on
        # the wire. Setting one here would either be overwritten or shadow the
        # injection; either way it means a credential leaked into the container.
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as http_err:
            raise _classify(http_err) from http_err
        except TimeoutError as timeout_err:
            # A timeout during response.read() surfaces as raw TimeoutError
            # (socket.timeout is aliased to TimeoutError since Python 3.10);
            # normalize to URLError so callers see one transport-error type.
            # Mirrors byair_client per #28.
            raise urllib.error.URLError(f"timed out: {timeout_err}") from timeout_err

        if not raw:
            # 204 No Content — Calendar's DELETE. An empty body is success, not
            # a JSONDecodeError for every caller to guard.
            return {}
        return json.loads(raw.decode("utf-8"))


def _quote(value: object) -> str:
    """URL-quote an id that becomes a path segment.

    Calendar ids are routinely email addresses, so `safe=''` is load-bearing:
    the `@` and any `+` must reach Calendar percent-encoded rather than being
    read as URL syntax.
    """
    return urllib.parse.quote(str(value), safe="")


def _query_value(value):
    """Render one param value for the query string.

    JSON booleans must reach Google as `true`/`false`. Left alone, urlencode
    stringifies Python's bool as `True`/`False`, which Google REJECTS as a
    malformed value — and callers hand us real booleans (`singleEvents`).
    Serializing here rather than at each call site means one encoder for every
    caller. Lists are mapped element-wise so `doseq` still sees a list.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_query_value(v) for v in value]
    return value


def _classify(http_err: urllib.error.HTTPError) -> Exception:
    """Map an HTTPError to this module's error for its failure mode.

    Reads the body once — it is the only place the `access_restricted` marker
    appears, and it carries the reason Google actually sent
    (`rateLimitExceeded`, `insufficientPermissions`). Reading it is
    DESTRUCTIVE: an HTTPError handed back afterwards has `.read() == b""` and
    `str(e)` has lost the diagnostic. So the detail is folded into the message
    of whatever this returns, and nothing hands the drained original back.
    """
    try:
        raw = http_err.read()
    except OSError:
        raw = b""
    detail = raw.decode("utf-8", "replace")[:300]

    if http_err.code == 401:
        return GatewayNotInjecting(f"{GATEWAY_NOT_INJECTING_HINT} (Google said: {detail})")
    if http_err.code == 403 and "access_restricted" in detail:
        return TierAccessRestricted(TIER_ACCESS_RESTRICTED_HINT)
    reason = f"{http_err.reason} ({detail})" if detail else http_err.reason
    return GoogleCalendarError(
        f"Google Calendar returned {http_err.code}: {reason}",
        status_code=http_err.code,
    )
