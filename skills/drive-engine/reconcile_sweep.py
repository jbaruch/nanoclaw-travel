#!/usr/bin/env python3
"""Drive-engine precheck — the LIVE unified reconcile (airport + meeting drives).

The one engine that manages every `Drive:` block. On each ~30-min sweep it:

1. builds the airport drive legs from the byAir itinerary (storms suppressed,
   connections handled, origins resolved at the right instant);
2. builds the meeting drive legs from the calendar (drive-planner's proven scan,
   with travel-away suppression so a home drive is never invented while abroad);
3. diffs both against the calendar's current blocks in ONE reconcile; and
4. APPLIES the plan — creating, updating, and deleting its own blocks.

It does NOT touch legacy drive-planner / flight-assist blocks (`managed_legacy` is
empty): those are left for the operator to clean up, and the two old engines are
retired so they stop writing. The engine's own blocks carry the unified codec.

`build_plan` is the testable core (injected resolvers, no I/O). `main` wires the
real clients and is the OUTER PROCESS BOUNDARY — it fails CLOSED to a no-wake
payload on any error so a transient outage skips one sweep.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import ParsedBlock, parse_block  # noqa: E402
from calendar_apply import apply_plan  # noqa: E402
from engine import AirportInfo, build_reconcile_plan  # noqa: E402
from meeting_source import meeting_desired_blocks  # noqa: E402
from normalize import flight_from_byair  # noqa: E402
from reconcile import DesiredBlock, ReconcilePlan  # noqa: E402

RouteFn = Callable[[str, str], "timedelta | None"]

# The engine owns ONLY its own (unified) blocks for now; it never converges or
# deletes legacy drive-planner / flight-assist blocks (the operator cleans those
# up). Empty = touch no legacy generation.
_MANAGED_LEGACY: frozenset[str] = frozenset()

SWEEP_WINDOW = timedelta(days=14)


@dataclass(frozen=True)
class ResolvedAirport:
    iata: str | None
    flag: str | None = None
    delay_index: str | None = None


@dataclass(frozen=True)
class PlanResult:
    plan: ReconcilePlan
    skipped: tuple[str, ...] = field(default_factory=tuple)


def build_plan(
    *,
    flight_records: list[dict],
    resolve_airport: Callable[[int], ResolvedAirport | None],
    meeting_blocks: list[DesiredBlock],
    current_blocks: list[ParsedBlock],
    route: RouteFn,
    now: datetime,
    schedule: list[dict] | None = None,
    home_address: str | None = None,
    live_origin: str | None = None,
) -> PlanResult:
    """Assemble the combined (airport + meeting) reconcile plan. Pure over inputs.

    Airport legs are built from the byAir records (airports resolved via
    `resolve_airport`); the pre-built `meeting_blocks` are folded in as extra
    desired blocks so both diff against the calendar in one reconcile.
    """
    flights = []
    airport_info: dict[str, AirportInfo] = {}
    skipped: list[str] = []

    for record in flight_records:
        dep_id = record.get("dep_airport_id")
        arr_id = record.get("arr_airport_id")
        dep = resolve_airport(dep_id) if isinstance(dep_id, int) else None
        arr = resolve_airport(arr_id) if isinstance(arr_id, int) else None
        if dep is None or dep.iata is None or arr is None or arr.iata is None:
            skipped.append(f"flight {record.get('flight_id')}: unresolved airport(s)")
            continue
        try:
            flights.append(flight_from_byair(record, dep_iata=dep.iata, arr_iata=arr.iata))
        except ValueError as exc:
            skipped.append(str(exc))
            continue
        airport_info[dep.iata] = AirportInfo(flag=dep.flag, delay_index=dep.delay_index)
        airport_info[arr.iata] = AirportInfo(flag=arr.flag, delay_index=arr.delay_index)

    result = build_reconcile_plan(
        flights=flights,
        airport_info=airport_info,
        current_blocks=current_blocks,
        route=route,
        schedule=schedule,
        home_address=home_address,
        now=now,
        live_origin=live_origin,
        extra_desired=meeting_blocks,
        managed_legacy=_MANAGED_LEGACY,
    )
    return PlanResult(plan=result.plan, skipped=tuple(skipped) + result.skipped)


# --- real-client wiring (I/O) -----------------------------------------------


def _on_path(name: str) -> None:
    runtime = Path(f"/home/node/.claude/skills/tessl__{name}")
    target = runtime if runtime.is_dir() else _BUNDLE_DIR.parent / name
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


def _fresh_live_origin(now: datetime, max_age_minutes: int) -> str | None:
    _on_path("flight-assist")
    from state import read_current_location

    loc = read_current_location()
    if not loc:
        return None
    try:
        when = datetime.fromisoformat(str(loc.get("captured_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    age = (now - when.astimezone(timezone.utc)).total_seconds() / 60
    if age < 0 or age > max_age_minutes:
        return None
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if isinstance(lat, int | float) and isinstance(lng, int | float):
        return f"{lat},{lng}"
    return None


def main() -> int:
    """Run the live unified reconcile and APPLY it. Fails closed on any error.

    outer-boundary-process-contract:
      caller's silent-failure shape — the scheduler reads a non-zero exit OR
        malformed stdout as "don't wake this cycle";
      what this catch emits — a valid {"wake_agent": ...} payload on stdout
        (traceback on stderr) and exit 0;
      why propagation breaks the contract — an uncaught exception would exit
        non-zero / print no payload, silently disabling the sweep.
    """
    try:
        _on_path("flight-assist")
        _on_path("travel-core")
        _on_path("drive-planner")
        import urllib.error

        from airport_drive_inputs import airport_context
        from byair_client import ByAirClient
        from calendar_reconcile import _find_events_args, _items
        from composio_client import ComposioClient
        from fetch_events import CalendarFetcher
        from maps_client import MapsClient, MapsError
        from scan import scan
        from skip_state import load_active_skips
        from state import (
            MAX_LIVE_ORIGIN_AGE_MINUTES,
            read_active_flights,
            read_config,
            read_flight_state,
        )
        from trip_origin import (
            flight_summaries,
            flight_windows,
            load_travel_schedule,
            resolve_anchor,
        )

        now = datetime.now(timezone.utc)
        config = read_config() or {}
        home_config = config.get("home_address")
        home = home_config if isinstance(home_config, str) else None
        schedule = load_travel_schedule()

        maps = MapsClient.from_env()

        def route(origin: str, destination: str) -> timedelta | None:
            try:
                tt = maps.travel_time(origin, destination)
            except (MapsError, urllib.error.URLError, TimeoutError):
                return None
            seconds = (
                tt.in_traffic_seconds if tt.in_traffic_seconds is not None else tt.duration_seconds
            )
            return timedelta(seconds=seconds)

        # --- meeting side: fetch calendar, scan, build meeting blocks ---
        fetcher = CalendarFetcher.from_env()
        events = fetcher.fetch_window(time_min=now, time_max=now + SWEEP_WINDOW)
        skips = load_active_skips(now)

        def anchor_for(at: datetime) -> tuple[str | None, str | None]:
            anchor = resolve_anchor(schedule, at=at, home_address=home)
            return anchor.address, anchor.detail

        meetings = scan(
            events,
            now=now,
            home_address=home or "",
            skip_state=skips,
            anchor_for=anchor_for,
            flight_windows=flight_windows(schedule),
            flight_summaries=flight_summaries(schedule),
        )
        meeting_blocks, meeting_skipped = meeting_desired_blocks(meetings, route=route)

        # --- airport side: flights + airport facts ---
        records = [
            record
            for fid in read_active_flights()
            if (record := read_flight_state(fid)) is not None
        ]
        byair = ByAirClient.from_env()
        airport_cache: dict[int, ResolvedAirport] = {}

        def resolve_airport(airport_id: int) -> ResolvedAirport | None:
            if airport_id not in airport_cache:
                ctx = airport_context(byair.get_airport(airport_id))
                airport_cache[airport_id] = ResolvedAirport(
                    iata=ctx.code, flag=ctx.flag, delay_index=ctx.delay_index
                )
            return airport_cache[airport_id]

        # --- current blocks ---
        composio = ComposioClient.from_env()
        raw = composio.find_events(
            _find_events_args(
                calendar_id="primary",
                time_min=(now - timedelta(days=2)).isoformat(),
                time_max=(now + timedelta(days=21)).isoformat(),
            )
        )
        current_blocks = [b for b in (parse_block(e) for e in _items(raw)) if b is not None]

        live_origin = _fresh_live_origin(now, MAX_LIVE_ORIGIN_AGE_MINUTES)

        result = build_plan(
            flight_records=records,
            resolve_airport=resolve_airport,
            meeting_blocks=meeting_blocks,
            current_blocks=current_blocks,
            route=route,
            now=now,
            schedule=schedule,
            home_address=home,
            live_origin=live_origin,
        )

        # --- APPLY (write) ---
        applied = apply_plan(result.plan, composio=composio, calendar_id="primary")

        skipped = list(result.skipped) + list(meeting_skipped)
        for line in skipped:
            print(f"[drive-engine] skip: {line}", file=sys.stderr)
        for line in applied.errors:
            print(f"[drive-engine] error: {line}", file=sys.stderr)

        payload = {
            "wake_agent": applied.total_writes > 0,
            "data": {
                "applied": {
                    "created": applied.created,
                    "updated": applied.updated,
                    "deleted": applied.deleted,
                    "converted": applied.converted,
                },
                "skipped": len(skipped),
                "errors": len(applied.errors),
            },
        }
        print(json.dumps(payload))
        return 0
    # outer-boundary-process-contract:
    #   caller's silent-failure shape — the scheduler reads a non-zero exit OR
    #     malformed stdout as "don't wake this cycle";
    #   what this catch emits — a valid {"wake_agent": false, ...} payload on
    #     stdout (traceback on stderr) and exit 0, so the sweep is skipped cleanly;
    #   why propagation breaks the contract — an uncaught exception would exit
    #     non-zero and print no payload, silently disabling the sweep.
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"drive-engine precheck failed, no wake: {exc}", file=sys.stderr)
        print(json.dumps({"wake_agent": False, "data": {"error": str(exc)}}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
