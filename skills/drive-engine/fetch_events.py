"""Wide-window primary-calendar event fetch for the drive-engine reconcile sweep.

The sweep needs the upcoming calendar events in one shot so `scan.py` can
classify them (Epic #59 §4). This module is that fetch: an events.list over
the primary calendar (singleEvents, a wide `[timeMin, timeMax]` window),
returning the raw Google Calendar event dicts in the exact shape
`scan(events=...)` consumes (`id`, `summary`, `location`, `start`, `end`,
`description`). Block state rides in that same `description` field (Epic #59
§4 — the calendar event IS the state, fetched by API), so it comes back
verbatim: `exclude_drive_block_events` reads it to drop the engine's own
blocks from the scan input, and `scan.py` reads it to recognize a legacy
drive-planner block by its marker.

This used to be a second, self-contained Composio transport — its own HTTP
POST, its own auth headers, its own success/failure envelope handling, its own
pagination loop and its own error type — sitting next to flight-assist's. Both
were the same calls to the same API, so #638 collapsed them: the transport,
the page draining (including the `maxResults` + bound that keep #171 fixed),
and the error taxonomy now come from `google_calendar_client`, and what is
left here is this fetch's own — the window contract and the projection down
to what `scan.py` reads.

`GoogleCalendarClient` ships in the co-located flight-assist bundle; it is
imported via the runtime-mount-with-dev-fallback pattern, the same way
`reconcile_sweep.py` reaches its own cross-bundle imports.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from fetch_events import CalendarFetcher

    fetcher = CalendarFetcher()
    events = fetcher.fetch_window(time_min=now, time_max=now + timedelta(days=14))
    results = scan(events, now=now, home_address=home, skip_state=active)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
_FLIGHT_ASSIST_RUNTIME = Path("/home/node/.claude/skills/tessl__flight-assist")
_FLIGHT_ASSIST_DEV = _BUNDLE_DIR.parent / "flight-assist"


def _flight_assist_dir() -> Path:
    if _FLIGHT_ASSIST_RUNTIME.is_dir():
        return _FLIGHT_ASSIST_RUNTIME
    if _FLIGHT_ASSIST_DEV.is_dir():
        return _FLIGHT_ASSIST_DEV
    raise FileNotFoundError(
        "drive-engine fetch_events: cannot locate the co-shipped flight-assist skill at "
        f"{_FLIGHT_ASSIST_RUNTIME} (runtime) or {_FLIGHT_ASSIST_DEV} (dev) — "
        "google_calendar_client ships there; both skills are part of jbaruch/nanoclaw-travel"
    )


if str(_flight_assist_dir()) not in sys.path:
    sys.path.insert(0, str(_flight_assist_dir()))

from google_calendar_client import (  # noqa: E402
    GoogleCalendarClient,
    GoogleCalendarError,
)

# The primary calendar is the operator's main one (where meetings live).
# `singleEvents` expands recurring events into instances so a weekly standup
# surfaces as datable occurrences `scan.py` can classify.
_BASE_ARGS = {"calendar_id": "primary", "singleEvents": True}

# Event fields carried through verbatim from the raw event. `scan.py` reads
# id/summary/location/start/end/description — `description` is where every
# generation of drive block keeps its marker and machine state (Epic #59 §4 —
# calendar event IS the state, fetched by API), so it is what lets
# `exclude_drive_block_events` and `scan` tell a block from a meeting.
# `extendedProperties` is the #178 migration target: `block_codec.parse_block`
# reads a block's state from `extendedProperties.private` first, the description
# second, so this projection must carry it through or the extended-properties
# branch would never see it once the writer flips. Dormant until then — no live
# block writes it yet.
_EVENT_FIELDS = (
    "id",
    "summary",
    "location",
    "start",
    "end",
    "description",
    "extendedProperties",
    "attendees",  # scan.py reads the operator's RSVP to skip declined meetings
    "status",  # scan.py skips cancelled events
)


class CalendarFetcher:
    """The drive-engine reconcile sweep's primary-calendar fetch.

    Holds a `GoogleCalendarClient` (no credential — the OneCLI gateway injects
    the Bearer on the wire; see that module). Not thread-safe — one instance
    per process.
    """

    def __init__(self, *, client=None, timeout: float = 30.0):
        self._client = client if client is not None else GoogleCalendarClient(timeout=timeout)

    def fetch_window(self, *, time_min: datetime, time_max: datetime) -> list:
        """Fetch primary-calendar events in [time_min, time_max] as scan-shaped dicts.

        The client drains the whole window across pages, so a busy calendar can
        never come back truncated and leave the sweep planning against a
        partial view (#171).

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
            GoogleCalendarError: on an API-level failure (incl. a window that
                will not drain within the client's page bound).
            urllib.error.URLError: on transport failure.
        """
        if time_min.tzinfo is None or time_max.tzinfo is None:
            raise ValueError("fetch_window: time_min and time_max must be timezone-aware")
        if time_max <= time_min:
            raise ValueError("fetch_window: time_max must be after time_min")

        resource = self._client.find_events(
            {**_BASE_ARGS, "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat()}
        )
        # `find_events` guarantees `items` (it merges every page into that one
        # shape), so there is nothing to probe for. The old `FetchError` guard
        # here existed because Composio's `data` envelope could carry the list
        # under any of several keys, or none, and a mis-read looked exactly
        # like an empty calendar — which would have made the sweep a silent
        # no-op. Malformed ENTRIES are still passed through untouched:
        # `_project_event` leaves a non-dict alone so `scan.py` can classify it
        # as `filtered` rather than have the fetch quietly drop it.
        return [_project_event(event) for event in resource["items"]]


def _project_event(event: object) -> object:
    """Keep only the fields scan.py reads, dropping the rest of the GCal resource.

    A non-dict entry is passed through untouched for `scan.py` to classify as
    `filtered` — fetch must not silently drop it (that would turn a shape
    regression into an invisible empty sweep).
    """
    if not isinstance(event, dict):
        return event
    return {field: event[field] for field in _EVENT_FIELDS if field in event}


# GoogleCalendarError is re-exported so callers that catch fetch failures can
# name the API-level type without reaching across bundles for it themselves.
__all__ = ["CalendarFetcher", "GoogleCalendarError"]
