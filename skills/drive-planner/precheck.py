#!/usr/bin/env python3
"""Drive-planner sweep precheck — the scheduler-invoked sweep gate.

The cadence-registry runs this every ~2h (the `cadence:` in SKILL.md). It is
the deterministic spine of the sweep (Epic #59 §3): fetch every upcoming
calendar event over a wide window, classify them with `scan.py`, and — for the
meetings that need a drive decision — pre-route each leg with live traffic and
build the exact calendar-block create-arguments. It wakes the agent ONLY when
there is a drive/skip decision to put to the user, handing the prepared blocks
across in `data` so the agent never routes or shapes a block itself (routing is
deterministic, so it lives here per `coding-policy: script-delegation`).

    {"wake_agent": <bool>, "data": {"meetings": [...], "skipped_routes": [...]}}

Each `meetings` entry is one actionable meeting with its summary, bucket, and
the per-leg `create_args` ready to pass to `GOOGLECALENDAR_CREATE_EVENT`. A leg
the router could not price is reported (with its error) rather than dropped —
the agent surfaces "couldn't compute drive time" instead of the planner going
silently blind (the meta-lesson of Epic #59 §5: no silent miss).

Already-handled meetings (`has_block`), live skips (`skipped`), past, and
filtered events never wake the agent — `scan.actionable()` keeps the gate to
`needs_decision` / `bridge` / `back_to_back` only.

Cross-bundle: `maps_client` ships in the co-located flight-assist skill (same
tile); this precheck imports it read-only via the runtime mount path with a
dev-clone fallback, the pattern `sync-tripit/precheck.py` uses. flight-assist's
own use of `maps_client` is untouched.

The script is the OUTER PROCESS BOUNDARY of the scheduled-task contract — the
scheduler reads non-zero exit OR malformed stdout as "don't wake this cycle".
The sole catch-all sits in `main()` and fails CLOSED (no wake) on an internal
error: a transient calendar/route outage skips one sweep, and the next ~2h
cron fire recovers — far better than waking the agent with nothing to do. Per
`coding-policy: error-handling` outer-boundary-process-contract carve-out.

stdlib-only (plus the in-tile maps_client) per `coding-policy:
dependency-management` (Stdlib First).
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BUNDLE_DIR))

# maps_client lives in the co-shipped flight-assist skill (same tile). Resolve
# its bundle at the runtime mount, falling back to the dev-clone sibling so the
# import works both on the NAS and in CI — same pattern as sync-tripit.
_FLIGHT_ASSIST_RUNTIME = Path("/home/node/.claude/skills/tessl__flight-assist")
_FLIGHT_ASSIST_DEV = _BUNDLE_DIR.parent / "flight-assist"

from block_props import DEFAULT_ARRIVAL_BUFFER_SECONDS, build_block_args  # noqa: E402
from fetch_events import CalendarFetcher  # noqa: E402
from home_address import read_current_home  # noqa: E402
from route_error import RouteError  # noqa: E402
from scan import MeetingClass, TransitLeg, actionable, scan  # noqa: E402
from skip_state import load_active_skips  # noqa: E402

# How far ahead the sweep scans for meetings needing a drive decision. Wide
# enough to surface a decision with comfortable lead time, bounded so the
# fetch stays cheap. A black-box constant (per `coding-policy:
# script-as-black-box`).
SWEEP_WINDOW = timedelta(days=14)

# Default calendar the planner writes blocks to when the fetch does not
# attribute an event to a specific calendar. "primary" is the operator's main
# Google Calendar.
DEFAULT_CALENDAR_ID = "primary"


def _load_maps_client():
    """Import and construct the in-tile MapsClient from env, cross-bundle.

    Raises FileNotFoundError when neither the runtime mount nor the dev sibling
    holds flight-assist (both skills ship from the same tile) — main()'s
    outer-boundary handler converts that into the safe no-wake payload.
    """
    if _FLIGHT_ASSIST_RUNTIME.is_dir():
        flight_assist_dir = _FLIGHT_ASSIST_RUNTIME
    elif _FLIGHT_ASSIST_DEV.is_dir():
        flight_assist_dir = _FLIGHT_ASSIST_DEV
    else:
        raise FileNotFoundError(
            "drive-planner sweep: cannot locate the co-shipped flight-assist skill at "
            f"{_FLIGHT_ASSIST_RUNTIME} (runtime) or {_FLIGHT_ASSIST_DEV} (dev) — maps_client "
            "ships there; both skills are part of jbaruch/nanoclaw-travel"
        )
    sys.path.insert(0, str(flight_assist_dir))
    from maps_client import MapsClient

    return MapsClient.from_env()


def _route_seconds(client, origin: str, destination: str) -> int:
    """Live drive seconds for one leg, preferring the in-traffic estimate.

    Translates the provider's `MapsError` / `urllib` transport failure into a
    `RouteError` so the pure planner catches one specific type (per
    `coding-policy: error-handling`) and records the leg as un-priced rather
    than dropping the meeting. `maps_client` is already on `sys.path` here —
    `_load_maps_client` inserted the flight-assist bundle before this runs.
    """
    from maps_client import MapsError

    try:
        result = client.travel_time(origin=origin, destination=destination)
    except (MapsError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RouteError(str(exc)) from exc
    if result.in_traffic_seconds is not None:
        return result.in_traffic_seconds
    return result.duration_seconds


def _leg_create_args(
    meeting: MeetingClass,
    leg: TransitLeg,
    *,
    home_address: str,
    baseline_seconds: int,
    calendar_id: str,
    buffer_seconds: int,
) -> dict:
    """Build the create-args for one priced leg.

    Outbound / bridge are arrival-anchored: the block starts `baseline +
    buffer` before the meeting and the recheck poll watches it. A return leg
    has no arrival deadline: it starts when the meeting ends and is created as
    a Free block for visibility, but `direction="return"` tells the poll to
    skip rechecking it (no deadline to miss on the way home in Phase 1).
    """
    summary = f"Drive: {meeting.summary}".strip()
    if leg.direction == "return":
        leg_start = leg.depart_after or meeting.end
        if leg_start is None:
            raise ValueError(f"return leg for {meeting.meeting_id} has no departure anchor")
        leg_end = leg_start + timedelta(seconds=baseline_seconds)
        arrive_by = leg_end
    else:
        arrive_by = leg.arrive_by or meeting.start
        if arrive_by is None:
            raise ValueError(f"{leg.direction} leg for {meeting.meeting_id} has no arrival anchor")
        leg_start = arrive_by - timedelta(seconds=baseline_seconds + buffer_seconds)
        leg_end = arrive_by

    return build_block_args(
        calendar_id=calendar_id,
        meeting_id=meeting.meeting_id,
        direction=leg.direction,
        summary=summary,
        leg_start=leg_start,
        arrive_by=arrive_by,
        baseline_seconds=baseline_seconds,
        origin=leg.origin or home_address,
        destination=leg.destination or home_address,
        leg_end=leg_end,
    )


def plan_meetings(
    results: list[MeetingClass],
    *,
    route,
    home_address: str,
    calendar_id: str = DEFAULT_CALENDAR_ID,
    buffer_seconds: int = DEFAULT_ARRIVAL_BUFFER_SECONDS,
) -> dict:
    """Turn classified events into the agent-facing sweep payload. Pure.

    `route(origin, destination)` returns live drive seconds or raises on a
    routing failure. Every actionable meeting is included; a leg the router
    could not price is recorded under `route_errors` on that meeting (with the
    leg + message) instead of silently dropping it.

    Returns `{"meetings": [...]}` where each meeting carries `meeting_id`,
    `summary`, `bucket`, `create_args` (one per priced leg), and `route_errors`.
    """
    meetings: list[dict] = []
    for meeting in actionable(results):
        create_args: list[dict] = []
        route_errors: list[dict] = []
        for leg in meeting.legs:
            origin = leg.origin or home_address
            destination = leg.destination or home_address
            try:
                baseline = route(origin, destination)
            except RouteError as exc:
                # A leg the router can't price is recorded, not dropped (no
                # silent miss, §5). A non-routing failure (e.g. a leg with no
                # anchor) is not a RouteError and propagates as a real bug.
                route_errors.append(
                    {
                        "direction": leg.direction,
                        "origin": leg.origin,
                        "destination": leg.destination,
                        "error": str(exc),
                    }
                )
                continue
            create_args.append(
                _leg_create_args(
                    meeting,
                    leg,
                    home_address=home_address,
                    baseline_seconds=baseline,
                    calendar_id=calendar_id,
                    buffer_seconds=buffer_seconds,
                )
            )
        meetings.append(
            {
                "meeting_id": meeting.meeting_id,
                "summary": meeting.summary,
                "bucket": meeting.bucket,
                "location": meeting.location,
                "start": meeting.start.isoformat() if meeting.start else None,
                "create_args": create_args,
                "route_errors": route_errors,
            }
        )
    return {"meetings": meetings}


def main() -> int:
    # outer-boundary-process-contract: the scheduler reads non-zero exit OR
    # malformed stdout as wake_agent=false. Every unexpected exception flows
    # into a safe-shape no-wake payload + exit 0. This handler fails CLOSED
    # (no wake): a transient calendar/route outage skips one sweep and the next
    # ~2h cron fire recovers — waking the agent with nothing actionable would
    # be noise. See `coding-policy: error-handling`. Sole catch-all in the file.
    try:
        now = datetime.now(timezone.utc)
        home_address = read_current_home()
        fetcher = CalendarFetcher.from_env()
        events = fetcher.fetch_window(time_min=now, time_max=now + SWEEP_WINDOW)
        skips = load_active_skips(now)
        results = scan(events, now=now, home_address=home_address, skip_state=skips)
        client = _load_maps_client()
        payload_data = plan_meetings(
            results,
            route=lambda o, d: _route_seconds(client, o, d),
            home_address=home_address,
        )
        wake = bool(payload_data["meetings"])
        payload = {"wake_agent": wake, "data": payload_data}
    except Exception:  # noqa: BLE001 — outer-boundary-process-contract
        traceback.print_exc(file=sys.stderr)
        payload = {"wake_agent": False, "data": {"reason": "sweep_precheck_internal_error"}}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
