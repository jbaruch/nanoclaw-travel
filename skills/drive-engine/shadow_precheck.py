#!/usr/bin/env python3
"""Drive-engine SHADOW precheck — read-only dry run of the unified reconcile.

Ships shadow-first per #156 R4: this assembles the desired legs from the byAir
itinerary, fetches the current primary-calendar drive blocks, computes the
reconcile diff, and LOGS it — it never creates, updates, or deletes an event. It
is the validation harness the owner runs against the live calendar to confirm the
delete-diff matches the counted garbage BEFORE any write path is enabled.

Two layers:

- `build_shadow_result` — the pure-ish core: takes normalized inputs + injected
  resolvers (airport facts, router, current blocks) and returns the plan, its
  rendered diff, counts, and the precheck payload. Fully unit-testable with fakes.
- `main` — wires the real clients (byAir, Maps, Composio calendar fetch, state /
  schedule readers) and calls the core, printing the shadow log to stderr and the
  precheck JSON to stdout. It is the OUTER PROCESS BOUNDARY of the scheduled-task
  contract and fails CLOSED (no wake) on any internal error.

The precheck payload's `wake_agent` is always False in shadow mode: this run
observes and logs, it never wakes the agent. `data.counts` carries the diff
summary for at-a-glance inspection.
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

from block_codec import ParsedBlock  # noqa: E402
from engine import AirportInfo, EngineResult, build_reconcile_plan  # noqa: E402
from normalize import flight_from_byair  # noqa: E402
from reconcile import ReconcilePlan  # noqa: E402
from shadow import plan_counts, render_plan  # noqa: E402

RouteFn = Callable[[str, str], "timedelta | None"]


@dataclass(frozen=True)
class ResolvedAirport:
    """A byAir airport resolved to what the engine needs: IATA code + buffer facts."""

    iata: str | None
    flag: str | None = None
    delay_index: str | None = None


@dataclass(frozen=True)
class ShadowResult:
    plan: ReconcilePlan
    rendered: str
    counts: dict[str, int]
    payload: dict
    skipped: tuple[str, ...] = field(default_factory=tuple)


def build_shadow_result(
    *,
    flight_records: list[dict],
    resolve_airport: Callable[[int], ResolvedAirport | None],
    current_blocks: list[ParsedBlock],
    route: RouteFn,
    now: datetime,
    schedule: list[dict] | None = None,
    home_address: str | None = None,
    live_origin: str | None = None,
    boarding_present: Callable | None = None,
) -> ShadowResult:
    """Assemble the desired legs from byAir records and diff against the calendar.

    Each record's dep/arr airports are resolved to IATA + facts via
    `resolve_airport`; a record with an unresolvable airport is skipped with a
    diagnostic rather than guessed. The result is rendered for the shadow log and
    packed into a no-wake precheck payload.
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

    result: EngineResult = build_reconcile_plan(
        flights=flights,
        airport_info=airport_info,
        current_blocks=current_blocks,
        route=route,
        schedule=schedule,
        home_address=home_address,
        now=now,
        live_origin=live_origin,
        boarding_present=boarding_present,
    )
    all_skipped = tuple(skipped) + result.skipped
    counts = plan_counts(result.plan)
    rendered = render_plan(result.plan)
    payload = {
        "wake_agent": False,
        "data": {"counts": counts, "skipped": list(all_skipped)},
    }
    return ShadowResult(
        plan=result.plan,
        rendered=rendered,
        counts=counts,
        payload=payload,
        skipped=all_skipped,
    )


# --- real-client wiring (I/O; shadow-only, never writes) --------------------


def _travel_core_on_path() -> None:
    travel_core = Path("/home/node/.claude/skills/tessl__travel-core")
    if not travel_core.is_dir():
        travel_core = _BUNDLE_DIR.parent / "travel-core"
    if str(travel_core) not in sys.path:
        sys.path.insert(0, str(travel_core))


def _flight_assist_on_path() -> None:
    fa = Path("/home/node/.claude/skills/tessl__flight-assist")
    if not fa.is_dir():
        fa = _BUNDLE_DIR.parent / "flight-assist"
    if str(fa) not in sys.path:
        sys.path.insert(0, str(fa))


def _fresh_live_origin(now: datetime, max_age_minutes: int) -> str | None:
    """The fresh live-GPS fix as `"<lat>,<lng>"`, or None if stale / absent.

    Only the fresh fix — NOT the home fallback — so the engine's imminence overlay
    decides plan-vs-GPS itself (the home case is already the planned origin).
    """
    _flight_assist_on_path()
    from state import read_current_location

    loc = read_current_location()
    if not loc:
        return None
    captured = loc.get("captured_at")
    try:
        when = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
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
    """Run the shadow reconcile against the live calendar; print log + payload.

    outer-boundary-process-contract: the scheduler reads non-zero exit OR
    malformed stdout as "don't wake". The sole catch-all here fails CLOSED — it
    emits a safe no-wake payload and exits 0 so a transient outage skips one shadow
    run rather than wedging. Never writes to the calendar. Per `coding-policy:
    error-handling` outer-boundary carve-out.
    """
    try:
        _flight_assist_on_path()
        _travel_core_on_path()
        import urllib.error

        from airport_drive_inputs import airport_context
        from byair_client import ByAirClient
        from calendar_reconcile import _find_events_args, _items
        from composio_client import ComposioClient
        from maps_client import MapsClient, MapsError
        from state import (
            MAX_LIVE_ORIGIN_AGE_MINUTES,
            read_active_flights,
            read_config,
            read_flight_state,
        )
        from trip_origin import load_travel_schedule, resolve_effective_home

        now = datetime.now(timezone.utc)
        config = read_config() or {}
        home = resolve_effective_home(config.get("home_address"), now=now)
        schedule = load_travel_schedule()
        records = [
            record
            for fid in read_active_flights()
            if (record := read_flight_state(fid)) is not None
        ]

        byair = ByAirClient.from_env()
        maps = MapsClient.from_env()
        composio = ComposioClient.from_env()

        airport_cache: dict[int, ResolvedAirport] = {}

        def resolve_airport(airport_id: int) -> ResolvedAirport | None:
            if airport_id not in airport_cache:
                ctx = airport_context(byair.get_airport(airport_id))
                airport_cache[airport_id] = ResolvedAirport(
                    iata=ctx.code, flag=ctx.flag, delay_index=ctx.delay_index
                )
            return airport_cache[airport_id]

        def route(origin: str, destination: str) -> timedelta | None:
            try:
                tt = maps.travel_time(origin, destination)
            except (MapsError, urllib.error.URLError):
                return None  # a route failure degrades one leg, not the whole run
            seconds = (
                tt.in_traffic_seconds if tt.in_traffic_seconds is not None else tt.duration_seconds
            )
            return timedelta(seconds=seconds)

        # Fetch the current primary-calendar drive blocks over a generous window
        # and parse each (new + both legacy shapes). READ ONLY.
        window_min = (now - timedelta(days=2)).isoformat()
        window_max = (now + timedelta(days=21)).isoformat()
        raw = composio.find_events(
            _find_events_args(calendar_id="primary", time_min=window_min, time_max=window_max)
        )
        current_blocks = [b for b in (_parse(e) for e in _items(raw)) if b is not None]

        live_origin = _fresh_live_origin(now, MAX_LIVE_ORIGIN_AGE_MINUTES)

        result = build_shadow_result(
            flight_records=records,
            resolve_airport=resolve_airport,
            current_blocks=current_blocks,
            route=route,
            now=now,
            schedule=schedule,
            home_address=home,
            live_origin=live_origin,
        )
        print(result.rendered, file=sys.stderr)
        if result.skipped:
            print(f"[shadow] skipped {len(result.skipped)} leg(s):", file=sys.stderr)
            for line in result.skipped:
                print(f"  - {line}", file=sys.stderr)
        print(json.dumps(result.payload))
        return 0
    # outer-boundary-process-contract:
    #   caller's silent-failure shape — the scheduler reads a non-zero exit OR
    #     malformed stdout as "don't wake this cycle";
    #   what this catch emits — a valid {"wake_agent": false, ...} payload on
    #     stdout (traceback to stderr) and exit 0, so the run is skipped cleanly;
    #   why propagation breaks the contract — an uncaught exception would exit
    #     non-zero and print no payload, silently disabling the shadow run.
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"drive-engine shadow precheck failed, no wake: {exc}", file=sys.stderr)
        print(json.dumps({"wake_agent": False, "data": {"error": str(exc)}}))
        return 0


def _parse(event: object) -> ParsedBlock | None:
    from block_codec import parse_block

    return parse_block(event)


if __name__ == "__main__":
    sys.exit(main())
