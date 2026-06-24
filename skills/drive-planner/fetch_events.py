"""Wide-window "all calendars" event fetch for the drive-planner sweep.

The sweep needs every upcoming calendar event in one shot so `scan.py` can
classify them (Epic #59 §4). This module is that fetch: a single Composio
tool-execution call against `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS` over
a time window, returning the raw Google Calendar event dicts in the exact
shape `scan(events=...)` consumes (`id`, `summary`, `location`, `start`,
`end`, `description`), plus `extendedProperties` — the drive-planner block's
own machine-readable state that the recheck poll reads back off its marked
blocks (Epic #59 §4). drive-planner owns its own fetch rather than sharing
flight-assist's per-calendar `composio_client` — a different action, a
different skill bundle — but mirrors that module's transport faithfully:
stdlib-only `urllib`, HTTP-mockable in CI, the Composio success/failure
envelope, one client per process.

Composio executes the action with a single POST keyed by the slug:

    POST {base}/tools/execute/{action}
    headers: x-api-key: <key>, Content-Type: application/json
    body:    {"user_id": "<id>", "arguments": {"timeMin": "...", "timeMax": "..."}}
    -> 200   {"data": {...events...}, "successful": true,  "error": null}
    -> 200   {"data": {...}, "successful": false, "error": "..."}

The exact `data` container for the events list is Composio-toolkit-version
specific (like the action slugs in `composio_client.py`); the candidate
keys are isolated in `_EVENT_CONTAINER_KEYS` at the top of the file — verify
against the live toolkit when first wiring against the NAS. A `successful:
true` response whose `data` carries none of those keys raises `FetchError`
rather than silently returning zero events (a silent empty fetch would make
the sweep a no-op and quietly stop planning).

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Caveat: Composio is mid-retirement (nanoclaw#638 → OneCLI workspace MCP);
this fetch is the one piece that re-points later, same as `composio-fetch`.

Public API:
    from fetch_events import CalendarFetcher, FetchError

    fetcher = CalendarFetcher.from_env()
    events = fetcher.fetch_window(time_min=now, time_max=now + timedelta(days=14))
    results = scan(events, now=now, home_address=home, skip_state=active)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime

_DEFAULT_BASE_URL = "https://backend.composio.dev/api/v3"

# GoogleCalendar action slug. Isolated here so a slug rename in the live
# Composio toolkit is a one-line fix; verify against the live toolkit.
ACTION_LIST_ALL_EVENTS = "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS"

# Candidate keys under the Composio `data` envelope that hold the event
# list. The live toolkit's exact shape is version-specific; these cover the
# observed Google-style (`items`) and Composio-wrapped (`events`) shapes.
# Checked in order; the first present list wins.
_EVENT_CONTAINER_KEYS = ("events", "items")

# Event fields carried through verbatim from the raw event. `scan.py` reads
# id/summary/location/start/end/description; `extendedProperties` is the
# drive-planner block's machine-readable state (baseline drive seconds,
# arrive-by, fired recheck offsets) the recheck poll reads back off its own
# marked blocks (Epic #59 §4 — calendar event IS the state, fetched by API).
# scan.py ignores the field it does not read.
_EVENT_FIELDS = (
    "id",
    "summary",
    "location",
    "start",
    "end",
    "description",
    "extendedProperties",
)


class FetchError(Exception):
    """Raised when the calendar fetch fails at the tool level or returns an
    unrecognized shape.

    Distinct from a transport error (`urllib.error.*`, which propagates): a
    `FetchError` means Composio answered but the answer was a tool-level
    failure (`successful: false`) or a `successful: true` body whose `data`
    held no recognizable event container. The fix is to check the Composio
    connection / re-verify the action's response shape, not to retry.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CalendarFetcher:
    """Thin Composio client for the wide-window all-calendars event fetch.

    Auth (`x-api-key`) and user scoping (`user_id`) are fixed per instance.
    Not thread-safe — one instance per process, matching `composio_client`.
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
                "CalendarFetcher: api_key is empty — set COMPOSIO_API_KEY in the env "
                "(from https://app.composio.dev settings) or pass it explicitly"
            )
        if not user_id:
            raise ValueError(
                "CalendarFetcher: user_id is empty — set COMPOSIO_USER_ID in the env "
                "(the Composio entity the Google Calendar account is connected under) "
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
    ) -> CalendarFetcher:
        """Construct from COMPOSIO_API_KEY + COMPOSIO_USER_ID env vars.

        COMPOSIO_BASE_URL optionally overrides the endpoint; unset uses the
        public v3 backend.
        """
        api_key = os.environ.get(api_key_var, "")
        if not api_key:
            raise ValueError(
                f"CalendarFetcher.from_env: ${api_key_var} is unset — add the Composio API "
                f"key (https://app.composio.dev settings) to OneCLI vault and restart the container"
            )
        user_id = os.environ.get(user_id_var, "")
        if not user_id:
            raise ValueError(
                f"CalendarFetcher.from_env: ${user_id_var} is unset — add the Composio user/"
                f"entity id (the entity the Google Calendar account is connected under) to vault"
            )
        base_url = os.environ.get(base_url_var) or _DEFAULT_BASE_URL
        return cls(api_key, user_id, base_url=base_url, timeout=timeout)

    def fetch_window(self, *, time_min: datetime, time_max: datetime) -> list:
        """Fetch all-calendar events in [time_min, time_max] as scan-shaped dicts.

        Args:
            time_min: window start (tz-aware).
            time_max: window end (tz-aware, after time_min).

        Returns:
            A list of raw event dicts carrying the fields scan.py reads.
            Empty when the window genuinely has no events. Any non-dict entry
            in the upstream list is passed through verbatim for scan.py to
            classify as `filtered` — fetch never silently drops one.

        Raises:
            ValueError: on a naive datetime or time_max <= time_min.
            FetchError: on a Composio tool-level failure or an unrecognized
                successful-response shape.
            urllib.error.HTTPError / URLError: on transport failure.
        """
        if time_min.tzinfo is None or time_max.tzinfo is None:
            raise ValueError("fetch_window: time_min and time_max must be timezone-aware")
        if time_max <= time_min:
            raise ValueError("fetch_window: time_max must be after time_min")

        data = self._execute(
            ACTION_LIST_ALL_EVENTS,
            {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat()},
        )
        return [_project_event(event) for event in _extract_events(data)]

    def _execute(self, action: str, arguments: dict) -> dict:
        """Execute one Composio action; return its `data` payload.

        Mirrors `composio_client.ComposioClient.execute`: raises FetchError
        on `successful: false`, normalizes a read timeout to URLError.
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
            raise urllib.error.URLError(f"timed out: {timeout_err}") from timeout_err

        body = json.loads(raw)
        if not body.get("successful", False):
            data = body.get("data") or {}
            status_code = data.get("status_code") if isinstance(data, dict) else None
            message = body.get("error") or (data.get("message") if isinstance(data, dict) else None)
            raise FetchError(
                f"{action} failed: {message or 'Composio reported successful=false'}",
                status_code=status_code,
            )
        return body.get("data") or {}


def _extract_events(data: dict) -> list:
    """Pull the event list out of the Composio `data` envelope.

    Tries each `_EVENT_CONTAINER_KEYS` in order and returns the first that is
    a list — verbatim, including any non-dict entries. A `data` with none of
    the keys raises FetchError: a successful response we cannot read is a
    shape regression to surface, not a silent empty fetch that would stop the
    sweep planning. Individual malformed entries are NOT filtered here —
    `scan.py` classifies a non-dict event as `filtered` (it never crashes and
    never silently drops one), so preserving them keeps a partial shape
    regression visible in the sweep's audit rather than hidden.
    """
    if not isinstance(data, dict):
        raise FetchError(
            f"calendar fetch returned a non-object data payload: {type(data).__name__}"
        )
    for key in _EVENT_CONTAINER_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return value
    raise FetchError(
        "calendar fetch succeeded but no event list found under "
        f"{_EVENT_CONTAINER_KEYS} — verify the action's response shape against the live toolkit"
    )


def _project_event(event: object) -> object:
    """Keep only the fields scan.py reads, dropping the rest of the GCal resource.

    A non-dict entry is passed through untouched for `scan.py` to classify as
    `filtered` — fetch must not silently drop it (that would turn a shape
    regression into an invisible empty sweep).
    """
    if not isinstance(event, dict):
        return event
    return {field: event[field] for field in _EVENT_FIELDS if field in event}
