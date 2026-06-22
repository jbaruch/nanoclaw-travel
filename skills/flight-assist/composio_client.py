"""HTTP client for the Composio tool-execution REST API (v3).

Used by the calendar `reconcile` script to execute `GOOGLECALENDAR_*`
actions deterministically — list calendars, find events in a time window,
create / patch / delete events — without the agent in the loop. Mirrors
`byair_client.py` / `maps_client.py`: stdlib-only `urllib`, HTTP-mockable
in CI, one client per process.

Composio executes every action with a single POST keyed by the action
slug:

    POST {base}/tools/execute/{action}
    headers: x-api-key: <key>, Content-Type: application/json
    body:    {"user_id": "<id>", "arguments": {...}}
    -> 200  {"data": {...}, "successful": true,  "error": null, "log_id": "..."}
    -> 200  {"data": {"status_code": 404, "message": "..."},
             "successful": false, "error": "...", "log_id": "..."}

Note the envelope: a failed *tool* call still returns HTTP 200 with
`successful: false` and the upstream provider status in `data.status_code`
(e.g. 404 when deleting an already-gone event). HTTP-level failures (a bad
API key, Composio itself down) surface as `urllib.error.HTTPError`.

The per-action *argument* schemas (`GOOGLECALENDAR_CREATE_EVENT`'s exact
field names, etc.) are Composio-version-specific and resolved against the
live toolkit by the reconcile executor, which owns the planner-op ->
arguments mapping. This client stays a faithful transport: it injects auth
+ user scoping, names the action slug, and passes a Composio-shaped
`arguments` dict straight through. Only the slug constants below are baked
in here, isolated at the top of the file for easy correction.

stdlib-only: `urllib.request` + `json` per `jbaruch/coding-policy:
dependency-management` (Stdlib First).

Public API:
    # The skill bundle dir is added to sys.path at invocation time; this
    # module is imported by its bare name (matches nanoclaw-core's convention).
    from composio_client import ComposioClient, ComposioError

    client = ComposioClient.from_env()
    calendars = client.list_calendars()
    events = client.find_events({"calendar_id": cid, "timeMin": lo, "timeMax": hi})
    client.delete_event({"calendar_id": cid, "event_id": eid})

Errors:
    - `ComposioError` wraps `successful: false` responses; `.status_code`
      exposes the upstream provider status (404 on a delete of an
      already-gone event, etc.) so callers can treat a vanished event as an
      idempotent no-op rather than a failure.
    - HTTP / network errors propagate as `urllib.error.URLError` /
      `urllib.error.HTTPError` per `jbaruch/coding-policy: error-handling`
      "Specific Exceptions".
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_DEFAULT_BASE_URL = "https://backend.composio.dev/api/v3"

# GoogleCalendar action slugs. Isolated here so a slug rename in the live
# Composio toolkit is a one-line fix; verify against the live toolkit when
# first wiring against the NAS (the reconcile executor probes these).
ACTION_LIST_CALENDARS = "GOOGLECALENDAR_LIST_CALENDARS"
ACTION_FIND_EVENTS = "GOOGLECALENDAR_FIND_EVENT"
ACTION_CREATE_EVENT = "GOOGLECALENDAR_CREATE_EVENT"
ACTION_PATCH_EVENT = "GOOGLECALENDAR_PATCH_EVENT"
ACTION_DELETE_EVENT = "GOOGLECALENDAR_DELETE_EVENT"


class ComposioError(Exception):
    """Raised when Composio returns `successful: false` for a tool call.

    `status_code` is the upstream provider's HTTP status when Composio
    reports one in `data.status_code` (e.g. 404 from Google Calendar on a
    missing event), else None. Callers gate idempotency on it — a delete
    that 404s means the event is already gone, which is success.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ComposioClient:
    """Thin REST client for the Composio v3 tool-execution endpoint.

    Auth (`x-api-key`) and user scoping (`user_id`) are fixed per client.
    Not thread-safe — one client per process is the intended shape, matching
    `byair_client.ByAirClient`.
    """

    def __init__(
        self,
        api_key: str,
        user_id: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise ValueError(
                "ComposioClient: api_key is empty — set COMPOSIO_API_KEY in the env "
                "(from https://app.composio.dev settings) or pass it explicitly"
            )
        if not user_id:
            raise ValueError(
                "ComposioClient: user_id is empty — set COMPOSIO_USER_ID in the env "
                "(the Composio entity/user the Google Calendar account is connected under) "
                "or pass it explicitly"
            )
        self._api_key = api_key
        self._user_id = user_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @classmethod
    def from_env(
        cls,
        *,
        api_key_var: str = "COMPOSIO_API_KEY",
        user_id_var: str = "COMPOSIO_USER_ID",
        base_url_var: str = "COMPOSIO_BASE_URL",
        timeout: float = 30.0,
    ) -> ComposioClient:
        """Construct from COMPOSIO_API_KEY + COMPOSIO_USER_ID env vars.

        COMPOSIO_BASE_URL optionally overrides the default endpoint; unset
        uses the public v3 backend.
        """
        api_key = os.environ.get(api_key_var, "")
        if not api_key:
            raise ValueError(
                f"ComposioClient.from_env: ${api_key_var} is unset — add the Composio API "
                f"key (https://app.composio.dev settings) to OneCLI vault and restart the container"
            )
        user_id = os.environ.get(user_id_var, "")
        if not user_id:
            raise ValueError(
                f"ComposioClient.from_env: ${user_id_var} is unset — add the Composio user/entity "
                f"id (the entity the Google Calendar account is connected under) to OneCLI vault"
            )
        base_url = os.environ.get(base_url_var) or _DEFAULT_BASE_URL
        return cls(api_key, user_id, base_url=base_url, timeout=timeout)

    # --- GoogleCalendar surface (thin slug-bound wrappers) ---------------

    def list_calendars(self, arguments: dict | None = None) -> dict:
        """List the user's calendars (`GOOGLECALENDAR_LIST_CALENDARS`)."""
        return self.execute(ACTION_LIST_CALENDARS, arguments or {})

    def find_events(self, arguments: dict) -> dict:
        """Find events by calendar + time window (`GOOGLECALENDAR_FIND_EVENT`)."""
        return self.execute(ACTION_FIND_EVENTS, arguments)

    def create_event(self, arguments: dict) -> dict:
        """Create a calendar event (`GOOGLECALENDAR_CREATE_EVENT`)."""
        return self.execute(ACTION_CREATE_EVENT, arguments)

    def patch_event(self, arguments: dict) -> dict:
        """Partial-update a calendar event (`GOOGLECALENDAR_PATCH_EVENT`)."""
        return self.execute(ACTION_PATCH_EVENT, arguments)

    def delete_event(self, arguments: dict) -> dict:
        """Delete a calendar event (`GOOGLECALENDAR_DELETE_EVENT`).

        A 404 surfaces as `ComposioError(status_code=404)`; the executor
        treats that as an idempotent success (event already gone).
        """
        return self.execute(ACTION_DELETE_EVENT, arguments)

    # --- transport -------------------------------------------------------

    def execute(self, action: str, arguments: dict) -> dict:
        """Execute one Composio action; return its `data` payload.

        Raises:
            ComposioError: on `successful: false` (tool-level failure);
                `.status_code` carries `data.status_code` when present.
            urllib.error.HTTPError: on HTTP-level failure (bad key, 5xx).
            urllib.error.URLError: on network/transport failure (incl. a
                body-read timeout, normalized for a single transport-error
                type per this module's contract).
        """
        url = f"{self._base_url}/tools/execute/{action}"
        payload = json.dumps({"user_id": self._user_id, "arguments": arguments}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": self._api_key,
        }
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except TimeoutError as timeout_err:
            # A timeout during response.read() surfaces as raw TimeoutError
            # (socket.timeout is aliased to TimeoutError since Python 3.10);
            # normalize to URLError so callers see one transport-error type.
            # Mirrors byair_client per #28.
            raise urllib.error.URLError(f"timed out: {timeout_err}") from timeout_err

        body = json.loads(raw)
        if not body.get("successful", False):
            data = body.get("data") or {}
            status_code = data.get("status_code") if isinstance(data, dict) else None
            message = body.get("error") or (data.get("message") if isinstance(data, dict) else None)
            raise ComposioError(
                f"{action} failed: {message or 'Composio reported successful=false'}",
                status_code=status_code,
            )
        return body.get("data") or {}
