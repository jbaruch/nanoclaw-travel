#!/usr/bin/env python3
"""Drive-engine precheck — the LIVE unified reconcile (airport + meeting drives).

The one engine that manages every `Drive:` block. On each ~30-min sweep it:

1. builds the airport drive legs from the byAir ∪ TripIt itinerary (R2 union, so a
   flight tracked by either source survives; storms suppressed, connections
   handled, origins resolved at the right instant), suppressing a trivial leg only
   when its boarding block exists on the byAir calendar (V3);
2. builds the meeting drive legs from the calendar (drive-planner's proven scan),
   masking flight events out by IDENTITY only (R5 — a ground meeting overlapping a
   redeye survives), with travel-away suppression so a home drive is never invented
   while abroad;
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
import time
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
from engine import AirportInfo, PlanBudgetExceeded, build_reconcile_plan  # noqa: E402
from flight_mask import flight_codes, is_flight_event, known_flight_codes  # noqa: E402
from meeting_source import exclude_drive_block_events, meeting_desired_blocks  # noqa: E402
from normalize import flight_from_byair  # noqa: E402
from reconcile import DesiredBlock, ReconcilePlan  # noqa: E402
from tripit_flights import flights_from_schedule  # noqa: E402

RouteFn = Callable[[str, str], "timedelta | None"]

# The engine owns ONLY its own (unified) blocks for now; it never converges or
# deletes legacy drive-planner / flight-assist blocks (the operator cleans those
# up). Empty = touch no legacy generation.
_MANAGED_LEGACY: frozenset[str] = frozenset()

SWEEP_WINDOW = timedelta(days=14)

# Wall-clock budget for the whole sweep. The host kills this precheck at ~33s
# (#164); bound the write phase so `apply_plan` stops starting new ops with margin
# for the last in-flight op + the JSON print + interpreter teardown, and returns a
# clean payload instead of being killed mid-write. Any ops it couldn't reach are
# `deferred` and drained on the next sweep (the reconcile is idempotent, so
# resuming never duplicates).
_SWEEP_WALL_CLOCK_BUDGET_SECONDS = 27.0

# Wall-clock budget for the plan (routing) phase, carved out of the sweep budget
# so the write phase still has room after it. Each airport leg can cost a slow
# provider failover (Google ZERO_RESULTS on an airport → three sequential TomTom
# calls), so an unbounded plan phase could route for minutes and blow past any
# container timeout (#172). `make_route` refuses to START a new route call once
# this budget is spent — it raises `PlanBudgetExceeded` instead, so the sweep
# takes its clean no-wake path rather than being killed mid-route before it can
# print JSON. On exhaustion the whole cycle skips cleanly (no partial plan — a
# partial `desired` set reads as orphaned blocks to delete). With memoization a
# normal itinerary never reaches this.
_PLAN_PHASE_BUDGET_SECONDS = 15.0

# Per-call timeout for the sweep's own maps client (the shared default is 10s).
# Tightened so a single `travel_time` — worst case one Google call plus three
# sequential TomTom fallback calls — cannot outlast the margin between the plan
# budget and the host precheck kill: a call that begins just before the budget
# elapses finishes in ≤ 4 × 4s = 16s, so 15s + 16s ≈ 31s stays under the ~33s
# kill and the clean no-wake JSON is always emitted first. A leg that times out is
# skipped this cycle and retried next sweep (the reconcile is idempotent).
_SWEEP_MAPS_TIMEOUT_SECONDS = 4.0


def make_route(
    maps,
    *,
    cache: dict[tuple[str, str], timedelta | None] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RouteFn:
    """A memoizing, budget-aware `route(origin, destination) -> timedelta | None`.

    Per `MapsClient`'s own contract ("cache aggressively at the caller level"),
    dedupes identical (origin, destination) pairs within one sweep — an airport
    that is both a departure destination and a transfer origin is routed once, not
    per leg. Traffic is stable across a single sweep, so a cached duration is the
    same answer the provider would return again (#172). A failed route caches None
    too, so a dead endpoint isn't re-attempted every leg.

    When `deadline` (a `clock()` reading) is set, a cache MISS past the deadline
    raises `PlanBudgetExceeded` BEFORE the network call — a single leg's provider
    fallback chain can't push the sweep past its budget after the per-leg poll
    already passed (#172). A cache HIT is free and always served, even past the
    deadline. `clock` is injected for deterministic tests.
    """
    import urllib.error

    from maps_client import MapsError

    memo: dict[tuple[str, str], timedelta | None] = {} if cache is None else cache

    def route(origin: str, destination: str) -> timedelta | None:
        key = (origin, destination)
        if key in memo:
            return memo[key]
        if deadline is not None and clock() >= deadline:
            # Deliberately no origin/destination in the message — it can be a home
            # address or live GPS fix, and this string is printed to stderr (#172).
            raise PlanBudgetExceeded("routing budget spent before a cache-miss route call")
        try:
            tt = maps.travel_time(origin, destination)
        except (MapsError, urllib.error.URLError, TimeoutError):
            memo[key] = None
            return None
        seconds = (
            tt.in_traffic_seconds if tt.in_traffic_seconds is not None else tt.duration_seconds
        )
        result = timedelta(seconds=seconds)
        memo[key] = result
        return result

    return route


@dataclass(frozen=True)
class ResolvedAirport:
    iata: str | None
    flag: str | None = None
    delay_index: str | None = None
    timezone: str | None = None


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
    tripit_flights: list | None = None,
    boarding_present: Callable | None = None,
) -> PlanResult:
    """Assemble the combined (airport + meeting) reconcile plan. Pure over inputs.

    Airport legs are built from the byAir records (airports resolved via
    `resolve_airport`) UNIONED with `tripit_flights` (already-normalized TripIt
    segments, R2) so a flight tracked by either source survives; the pre-built
    `meeting_blocks` are folded in as extra desired blocks so both diff against
    the calendar in one reconcile. `boarding_present` gates trivial-leg
    suppression (V3).
    """
    flights = list(tripit_flights or [])
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
        airport_info[dep.iata] = AirportInfo(
            flag=dep.flag, delay_index=dep.delay_index, timezone=dep.timezone
        )
        airport_info[arr.iata] = AirportInfo(
            flag=arr.flag, delay_index=arr.delay_index, timezone=arr.timezone
        )

    result = build_reconcile_plan(
        flights=flights,
        airport_info=airport_info,
        current_blocks=current_blocks,
        route=route,
        schedule=schedule,
        home_address=home_address,
        now=now,
        live_origin=live_origin,
        boarding_present=boarding_present,
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


def _event_end(event: dict) -> datetime | None:
    end = event.get("end")
    raw = end.get("dateTime") if isinstance(end, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _boarding_block_end_times(composio, find_events_args, items, byair_calendar_id, now):
    """End instants of boarding blocks on the byAir calendar, for trivial-leg
    suppression (V3 — the presence check lives on the byAir calendar, not primary).

    Returns [] when no byAir calendar is configured or the fetch fails — so a
    trivial leg is NOT suppressed without a confirmed boarding block (R6: never
    suppress the only 'head to the gate' signal silently)."""
    if not byair_calendar_id:
        return []
    import urllib.error

    from composio_client import ComposioError

    try:
        raw = composio.find_events(
            find_events_args(
                calendar_id=byair_calendar_id,
                time_min=(now - timedelta(hours=6)).isoformat(),
                time_max=(now + timedelta(days=21)).isoformat(),
            )
        )
    except (ComposioError, urllib.error.URLError):
        return []
    ends: list[datetime] = []
    for event in items(raw):
        if not isinstance(event, dict):
            continue
        summary = event.get("summary")
        if isinstance(summary, str) and summary.strip().lower().startswith("boarding"):
            end = _event_end(event)
            if end is not None:
                ends.append(end)
    return ends


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
        sweep_start = time.monotonic()
        _on_path("flight-assist")
        _on_path("travel-core")
        _on_path("drive-planner")

        from airport_drive_inputs import airport_context
        from byair_client import ByAirClient
        from calendar_reconcile import _find_events_args, _items
        from composio_client import ComposioClient
        from fetch_events import CalendarFetcher
        from home_address import HomeAddressError, read_current_home
        from maps_client import MapsClient
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
            load_travel_schedule,
            resolve_anchor,
        )

        now = datetime.now(timezone.utc)
        config = read_config() or {}
        # Home resolution (#162): the flight-assist config may have no
        # home_address key (a fresh cutover never provisioned it), so fall back to
        # the canonical user_profile current_home — the same source the retired
        # drive-planner read. Without this the sweep is DOA: the meeting scan
        # raises on an empty home and takes the whole cycle down.
        home_config = config.get("home_address")
        home = home_config if isinstance(home_config, str) and home_config.strip() else None
        if home is None:
            try:
                home = read_current_home()
            except HomeAddressError:
                home = None  # neither source configured — degrade, see below
        schedule = load_travel_schedule()

        maps = MapsClient.from_env(timeout=_SWEEP_MAPS_TIMEOUT_SECONDS)
        # Deadline for the routing phase, so a slow provider-failover storm can't
        # run routing for minutes (#172).
        plan_deadline = sweep_start + _PLAN_PHASE_BUDGET_SECONDS

        # One memoizing, budget-aware route closure for the whole sweep — meeting
        # legs and airport legs share it, so a repeated (origin, destination) pair
        # costs one provider round trip, not one per leg. It serves cached pairs
        # even past the deadline but refuses to START a new (cache-miss) route call
        # once the deadline passes, raising PlanBudgetExceeded instead (#172).
        route = make_route(maps, deadline=plan_deadline, clock=time.monotonic)

        # --- flight sources: byAir records + TripIt segments (R2 union) ---
        records = [
            record
            for fid in read_active_flights()
            if (record := read_flight_state(fid)) is not None
        ]
        tripit_flights = flights_from_schedule(schedule)

        # Known flight designators from the whole itinerary (both sources) — the
        # identity mask (R5) that keeps flight events out of the meeting scan.
        known_codes = known_flight_codes(
            [r.get("code") for r in records] + [f.code for f in tripit_flights]
        )
        for summary in flight_summaries(schedule):
            known_codes |= flight_codes(summary)

        composio = ComposioClient.from_env()

        # --- V3: boarding-block presence on the byAir calendar ---
        boarding_ends = _boarding_block_end_times(
            composio, _find_events_args, _items, config.get("byair_calendar_id"), now
        )

        def boarding_present(flight) -> bool:
            # A boarding block ends at ~departure; match it to this flight by time.
            dep = flight.effective_dep
            return any(abs((be - dep).total_seconds()) < 1800 for be in boarding_ends)

        # --- meeting side: fetch calendar, mask flights by IDENTITY (R5), scan ---
        # scan() requires a non-empty home_address. If neither the config nor the
        # user_profile provided one (#162), SKIP the meeting side with a
        # diagnostic rather than letting scan raise and take the airport side down
        # with it — the whole cycle must not be DOA over a missing home.
        meeting_blocks: list[DesiredBlock] = []
        meeting_skipped: list[str] = []
        if home:
            fetcher = CalendarFetcher.from_env()
            events = exclude_drive_block_events(
                fetcher.fetch_window(time_min=now, time_max=now + SWEEP_WINDOW)
            )
            # Drop flight events by identity only (R5 — never by time overlap, so a
            # ground meeting overlapping a redeye window survives). scan then runs
            # with an EMPTY flight context, since masking already happened here.
            events = [
                e
                for e in events
                if not (isinstance(e, dict) and is_flight_event(e.get("summary"), known_codes))
            ]
            skips = load_active_skips(now)

            def anchor_for(at: datetime) -> tuple[str | None, str | None]:
                anchor = resolve_anchor(schedule, at=at, home_address=home)
                return anchor.address, anchor.detail

            meetings = scan(
                events,
                now=now,
                home_address=home,
                skip_state=skips,
                anchor_for=anchor_for,
                flight_windows=[],
                flight_summaries=[],
            )
            meeting_blocks, meeting_skipped = meeting_desired_blocks(meetings, route=route)
        else:
            meeting_skipped = [
                "meeting side skipped: no home_address (flight-assist config and "
                "user_profile current_home both empty) — see #162"
            ]

        # Outcome-level budget gate (#172). Meeting routing above shares the
        # budget-aware `route`; a single meeting leg whose provider-fallback chain
        # began just under the deadline can return well past it. `make_route` stops
        # STARTING new route calls past the plan deadline, but the current-block
        # fetch and airport resolution below are non-route network work that would
        # still run. Gate on the whole-sweep budget (not the tighter plan deadline,
        # so cheap cache-served legs aren't needlessly abandoned): once even that is
        # spent, skip cleanly here rather than push more work toward the host kill.
        if time.monotonic() - sweep_start >= _SWEEP_WALL_CLOCK_BUDGET_SECONDS:
            raise PlanBudgetExceeded("sweep budget spent after meeting routing")

        # --- airport facts ---
        byair = ByAirClient.from_env()
        airport_cache: dict[int, ResolvedAirport] = {}

        def resolve_airport(airport_id: int) -> ResolvedAirport | None:
            if airport_id not in airport_cache:
                ctx = airport_context(byair.get_airport(airport_id))
                airport_cache[airport_id] = ResolvedAirport(
                    iata=ctx.code,
                    flag=ctx.flag,
                    delay_index=ctx.delay_index,
                    timezone=ctx.timezone,
                )
            return airport_cache[airport_id]

        # --- current blocks ---
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
            tripit_flights=tripit_flights,
            boarding_present=boarding_present,
            now=now,
            schedule=schedule,
            home_address=home,
            live_origin=live_origin,
        )

        # --- APPLY (write) ---
        # Give apply whatever of the sweep budget the fetch/plan phase left, so
        # the write phase stops with margin before the host precheck kill (#164).
        apply_budget = max(_SWEEP_WALL_CLOCK_BUDGET_SECONDS - (time.monotonic() - sweep_start), 0.0)
        applied = apply_plan(
            result.plan, composio=composio, calendar_id="primary", budget_seconds=apply_budget
        )

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
                "deferred": applied.deferred,
                "skipped": len(skipped),
                "errors": len(applied.errors),
            },
        }
        print(json.dumps(payload))
        return 0
    except PlanBudgetExceeded as exc:
        # Routing (meeting side or airport side — both share the budget-aware
        # `route`) ran past its budget. Skip this whole cycle cleanly rather than
        # apply a partial plan (a partial `desired` set reads as orphaned blocks to
        # delete). Handled at the outer boundary so it covers every `route` use in
        # main(), not just build_plan (#172). Next sweep resumes; idempotent.
        print(f"[drive-engine] {exc}; skipping cycle", file=sys.stderr)
        print(json.dumps({"wake_agent": False, "data": {"reason": "plan_budget_exceeded"}}))
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
