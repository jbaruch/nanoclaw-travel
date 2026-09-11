#!/usr/bin/env python3
"""List the operator's calendar events around a flight for the day-before check.

Used by SKILL.md Step 3's `day_before` row. Reads the flight's state, fetches
the primary calendar across the flight's conflict window, drops events the
operator declined or that were cancelled, and renders every time on the
operator's current clock — so the compose summarizes a list, and makes no
filtering or timezone decision of its own (#300). The rules live in
`day_before_calendar.py`; the zone comes from the core `current-tz` reader via
`travel-core/operator_tz.py`.

Usage:
    day-before-calendar.py <flight_id>

Stdout (single-line JSON):
    {"flight_id": N, "tz": "<iana>" | null,
     "window_start": "<ISO>", "window_end": "<ISO>",
     "events": [{"summary", "location", "all_day", "start", "end", "display"}],
     "skipped_declined_or_cancelled": N}
    {"error": "..."}        — the flight has no state on disk (exit 0, like
                              get-flight-state.py)
    {"error": "gateway"}    — the OneCLI gateway is not authenticating (exit 1)
    {"error": "tier"}       — this agent's tier is gated from Google (exit 1)
    {"error": "calendar"}   — Calendar or the network failed this call (exit 1)

`tz` is null when the operator's zone is unavailable; `start` / `end` then
keep each event's own offset. Exit 2 on usage errors.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent.parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

# operator_tz ships in the co-located travel-core bundle: runtime mount first,
# dev-clone sibling for CI (travel-core SKILL.md consumer contract).
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from calendar_reconcile import _find_events_args, _items  # noqa: E402
from day_before_calendar import calendar_conflicts, conflict_window  # noqa: E402
from google_calendar_client import (  # noqa: E402
    GatewayNotInjecting,
    GoogleCalendarClient,
    GoogleCalendarError,
    TierAccessRestricted,
)
from operator_tz import OperatorTz, read_operator_tz  # noqa: E402
from state import read_flight_state  # noqa: E402


def _emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")))


def run(
    flight_id: int,
    *,
    client,
    operator_tz_reader: Callable[[], OperatorTz | None] = read_operator_tz,
    read_state: Callable[[int], dict | None] = read_flight_state,
) -> int:
    """Do the work for one flight and print the result; return the exit code.

    `client`, `operator_tz_reader` and `read_state` are injected so tests run
    without the gateway, the core reader, or on-disk state.
    """
    state = read_state(flight_id)
    if state is None:
        _emit({"error": f"flight_id {flight_id} has no state on disk"})
        return 0
    window = conflict_window(state)
    try:
        raw = client.find_events(
            _find_events_args(
                calendar_id="primary",
                time_min=window[0].isoformat(),
                time_max=window[1].isoformat(),
            )
        )
    except GatewayNotInjecting as exc:
        print(f"day-before-calendar: unauthenticated — {exc}", file=sys.stderr)
        _emit({"error": "gateway"})
        return 1
    except TierAccessRestricted as exc:
        print(f"day-before-calendar: unavailable at this tier — {exc}", file=sys.stderr)
        _emit({"error": "tier"})
        return 1
    except (GoogleCalendarError, urllib.error.URLError) as exc:
        print(
            f"day-before-calendar: calendar read failed ({exc}) — the day-before check "
            "goes out without the calendar part; the next wake retries",
            file=sys.stderr,
        )
        _emit({"error": "calendar"})
        return 1
    zone = operator_tz_reader()
    tz = zone.tz if zone is not None else None
    events, skipped = calendar_conflicts(_items(raw), window=window, operator_tz=tz)
    _emit(
        {
            "flight_id": flight_id,
            "tz": tz,
            "window_start": window[0].isoformat(),
            "window_end": window[1].isoformat(),
            "events": events,
            "skipped_declined_or_cancelled": skipped,
        }
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: day-before-calendar.py <flight_id>", file=sys.stderr)
        return 2
    try:
        flight_id = int(argv[1])
    except ValueError:
        print(f"day-before-calendar: flight_id must be an int, got {argv[1]!r}", file=sys.stderr)
        return 2
    return run(flight_id, client=GoogleCalendarClient())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
