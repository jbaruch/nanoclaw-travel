#!/usr/bin/env python3
"""Drive-planner recheck poll — the scheduler-invoked traffic-growth gate.

This is the second cadence skill the poll model needs (Epic #59 §3, confirmed):
it fires every ~15 min and asks, for every drive block currently in its recheck
window, "did traffic grow enough since the block was created that the user must
leave earlier — or is it already time to go?" The sweep (the ~2h skill)
creates blocks; this poll watches them. There are no per-block one-off
scheduled rows to forget (lombot #48): the poll re-derives the work from the
blocks themselves every cycle, so a block can never silently lose its rechecks.

Calendar IS the state (Epic #59 §4): the poll re-fetches the near-term window
by a direct API call (never an agentic read), parses each of its own marked
blocks back into a `BlockState`, and reads the baseline drive seconds /
arrive-by / routed endpoints / prior-alert record straight off the event's
`description` (the live v3 toolkit has no writable extendedProperties). Only
arrival-anchored legs (outbound / bridge) are rechecked — a return leg home has
no deadline to miss in Phase 1.

For a due block the poll re-routes the leg with live traffic, runs the
`recheck.evaluate_recheck` gate, and fires each alert condition at most once via
`block_props.next_alerts`. For a firing block it emits a `patch` carrying the
block's full rebuilt `description` (the state lives there; `next_alerts` only
flips the alert record). The poll does NOT patch the calendar itself: the
suppression write is deferred to the SKILL.md, which calls `apply.py suppress`
ONLY after the ping is confirmed sent — a patch landing before a failed send
would permanently suppress a leave-earlier / leave-now alert, whereas a
forgotten patch merely re-pings next poll (the safe direction).

Cross-bundle: `fetch_events` / `block_props` / `recheck` ship in the co-located
drive-planner skill; `maps_client` in flight-assist. All imported read-only via
the runtime-mount-with-dev-fallback pattern.

Outer-boundary precheck: the scheduler reads non-zero exit OR malformed stdout
as wake_agent=false. The sole catch-all in `main()` fails CLOSED (no wake) on
an internal error — a transient calendar/route outage skips one poll and the
next ~15-min fire recovers. The leave-by ping is independently re-derived each
poll, so one skipped cycle never loses it permanently. Per `coding-policy:
error-handling` outer-boundary-process-contract carve-out.

stdlib-only (plus in-plugin modules) per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
_TILE_SKILLS = _BUNDLE_DIR.parent

_DRIVE_PLANNER_RUNTIME = Path("/home/node/.claude/skills/tessl__drive-planner")
_DRIVE_PLANNER_DEV = _TILE_SKILLS / "drive-planner"
_FLIGHT_ASSIST_RUNTIME = Path("/home/node/.claude/skills/tessl__flight-assist")
_FLIGHT_ASSIST_DEV = _TILE_SKILLS / "flight-assist"


def _resolve(runtime: Path, dev: Path, what: str) -> Path:
    if runtime.is_dir():
        return runtime
    if dev.is_dir():
        return dev
    raise FileNotFoundError(
        f"drive-planner recheck: cannot locate the co-shipped {what} skill at {runtime} "
        f"(runtime) or {dev} (dev) — all three skills ship from jbaruch/nanoclaw-travel"
    )


# drive-planner ships the shared scan/codec/gate; add its bundle to the path
# before importing them by bare name (same cross-bundle pattern as sync-tripit).
sys.path.insert(0, str(_resolve(_DRIVE_PLANNER_RUNTIME, _DRIVE_PLANNER_DEV, "drive-planner")))

from block_props import next_alerts, parse_block  # noqa: E402
from fetch_events import CalendarFetcher  # noqa: E402
from recheck import evaluate_recheck  # noqa: E402
from route_error import RouteError  # noqa: E402

# How far back / ahead the poll fetches to catch every block whose recheck
# window is open now. The window opens 45 min before a block's leave-by and
# closes 15 min after; fetching now−30m … now+90m covers it with margin. A
# black-box constant (per `coding-policy: script-as-black-box`).
RECHECK_FETCH_BEHIND = timedelta(minutes=30)
RECHECK_FETCH_AHEAD = timedelta(minutes=90)

# Leg directions the poll rechecks — arrival-anchored only. A return leg has no
# arrival deadline in Phase 1, so it is created for visibility but not watched.
_RECHECK_DIRECTIONS = ("outbound", "bridge")


def evaluate_blocks(events: list, *, now: datetime, route) -> dict:
    """Decide which due blocks should ping, and the suppression patches. Pure.

    `route(origin, destination)` returns live drive seconds or raises on a
    routing failure. For each fetched event that parses as a due, arrival-
    anchored drive block, re-route the leg, gate it, and fire each alert
    condition at most once. A leg the router cannot price is recorded under
    `route_errors` (no silent miss) rather than alerted on.

    Returns `{"alerts": [...], "patches": [...], "route_errors": [...]}`:
      - alerts: one per firing block — meeting id, block summary, the alert
        kinds, and the recomputed leave-by / drive delta for the ping.
      - patches: the suppression writes to apply (event id, calendar, new
        alerted record) so a later poll does not re-ping the same condition.
      - route_errors: blocks that were due but could not be priced this poll.
    """
    alerts: list = []
    patches: list = []
    route_errors: list = []

    for event in events:
        state = parse_block(event)
        if state is None or state.direction not in _RECHECK_DIRECTIONS:
            continue
        if not state.due_for_recheck(now):
            continue

        try:
            current_seconds = route(state.origin, state.destination)
        except RouteError as exc:
            # A leg the router can't price this poll is recorded, not silently
            # missed (§5); the next ~15-min poll retries. A non-routing bug is
            # not a RouteError and propagates.
            route_errors.append(
                {
                    "meeting_id": state.meeting_id,
                    "destination": state.destination,
                    "error": str(exc),
                }
            )
            continue

        decision = evaluate_recheck(
            baseline_seconds=state.baseline_seconds,
            current_seconds=current_seconds,
            arrive_by=state.arrive_by,
            now=now,
        )
        fire, new_alerted = next_alerts(
            state.alerted,
            grew=decision.grew_past_threshold,
            leave_now=decision.leave_by_passed,
        )
        if not fire:
            continue

        # Fall back to the meeting id when the fetched block has no summary, so
        # the alert never reads "Leave now for None".
        raw_summary = event.get("summary") if isinstance(event, dict) else None
        summary = raw_summary if isinstance(raw_summary, str) and raw_summary else state.meeting_id
        alerts.append(
            {
                "meeting_id": state.meeting_id,
                "summary": summary,
                "kinds": list(fire),
                "destination": state.destination,
                "current_seconds": decision.current_seconds,
                "delta_seconds": decision.delta_seconds,
                # Display-ready minutes so the SKILL.md carries no ÷60 formula
                # (per `coding-policy: script-as-black-box`).
                "current_minutes": round(decision.current_seconds / 60),
                "delta_minutes": round(decision.delta_seconds / 60),
                "new_leave_by": decision.new_leave_by.isoformat(),
                "seconds_until_leave_by": decision.seconds_until_leave_by,
                "reason": decision.reason,
            }
        )
        # Rebuild the block's full description with the updated alert record.
        # The state lives in the description, and `GOOGLECALENDAR_PATCH_EVENT`
        # supports a partial `description` update, so the patch is that one
        # field. The patch is applied AFTER the agent confirms the ping was
        # sent (the SKILL.md calls `apply.py suppress`), never here — a patch
        # landing before a failed send would permanently suppress the alert.
        patches.append(
            {
                "event_id": state.event_id,
                "calendar_id": state.calendar_id,
                "description": state.description_with_alerts(new_alerted),
            }
        )

    return {"alerts": alerts, "patches": patches, "route_errors": route_errors}


def should_wake(result: dict) -> bool:
    """Wake the agent on alerts OR route errors.

    A due block the router couldn't price is a traffic blind-spot worth
    surfacing — recording it in `data` without waking would make a routing
    outage invisible for that poll (per `coding-policy: script-delegation`
    precheck gating).
    """
    return bool(result.get("alerts")) or bool(result.get("route_errors"))


def _load_maps_client():
    flight_assist_dir = _resolve(_FLIGHT_ASSIST_RUNTIME, _FLIGHT_ASSIST_DEV, "flight-assist")
    sys.path.insert(0, str(flight_assist_dir))
    from maps_client import MapsClient

    return MapsClient.from_env()


def _route_seconds(client, origin: str, destination: str) -> int:
    # Translate the provider's MapsError / transport failure into a RouteError
    # so evaluate_blocks catches one specific type (per `coding-policy:
    # error-handling`). maps_client is on sys.path — _load_maps_client ran first.
    from maps_client import MapsError

    try:
        result = client.travel_time(origin=origin, destination=destination)
    except (MapsError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        # maps_client does a raw response.read() without normalizing a read
        # timeout to URLError, so catch TimeoutError too — otherwise it would
        # escape RouteError into the outer-boundary catch-all and silently drop
        # the whole poll.
        raise RouteError(str(exc)) from exc
    if result.in_traffic_seconds is not None:
        return result.in_traffic_seconds
    return result.duration_seconds


def main() -> int:
    # outer-boundary-process-contract: the scheduler reads non-zero exit OR
    # malformed stdout as wake_agent=false. Fails CLOSED on internal error —
    # a transient outage skips one ~15-min poll and the next recovers; the
    # leave-by alert is re-derived each poll so it is never lost permanently.
    # See `coding-policy: error-handling`. Sole catch-all in the file.
    try:
        now = datetime.now(timezone.utc)
        fetcher = CalendarFetcher.from_env()
        events = fetcher.fetch_window(
            time_min=now - RECHECK_FETCH_BEHIND, time_max=now + RECHECK_FETCH_AHEAD
        )
        maps = _load_maps_client()
        result = evaluate_blocks(events, now=now, route=lambda o, d: _route_seconds(maps, o, d))
        # The suppression patches ride along in `data`; the SKILL.md applies
        # them via `apply.py suppress` ONLY after the ping is confirmed sent, so
        # a failed send never permanently suppresses a leave-earlier / leave-now
        # alert (a forgotten patch merely re-pings next poll — the safe
        # direction). The precheck never patches the calendar itself.
        # Wake on alerts OR route_errors: a due block the router couldn't price
        # is a traffic blind-spot worth surfacing — recording it in `data`
        # without waking would make a routing outage invisible for that poll
        # (per `coding-policy: script-delegation` precheck gating).
        payload = {"wake_agent": should_wake(result), "data": result}
    except Exception:  # noqa: BLE001 — outer-boundary-process-contract
        traceback.print_exc(file=sys.stderr)
        payload = {"wake_agent": False, "data": {"reason": "recheck_precheck_internal_error"}}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
