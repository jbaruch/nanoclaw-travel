"""Assemble a flight's airport drive blocks from live byAir + Maps inputs.

Piece 4c of #90 (the integration), reconcile/route half — first slice. This is
the I/O-bearing layer that turns a flight's persisted state into the two
`DesiredDriveBlock`s the pure planner `airport_drive.plan_drive_block` reconciles
against the calendar:

    flight state ──► airport_drive_reconcile ──► [DesiredDriveBlock, ...] ──► plan_drive_block

For each direction it needs to build, it resolves the airport context
(`byair.get_airport` → flag / `delay.index` / IANA tz / code, via
`airport_drive_inputs.airport_context`), routes the drive leg
(`maps.travel_time`), picks the byAir-truth dep/arr instant from the snapshot,
and hands those to `airport_drive_inputs.departure_block` / `arrival_block`. The
window math, summaries, and tz selection live there; this module is the glue
that feeds them live data.

What it does NOT do yet (the next slice): fetch the primary calendar, run
`plan_drive_block`, or execute the create/shift ops via Composio. This is the
pure-of-calendar-I/O assembly half — given injected `byair` / `maps` clients and
an already-resolved `origin`, it returns the desired blocks; the orchestration
that fetches, plans, and writes lands on top of it.

Which blocks get built is gated on the flight's `computed_status`:
  * `to_airport` — while the flight has not left (scheduled / check-in / boarding).
  * `from_airport` — once it is in the air or down (departed / en_route / landed),
    so the drive-home block appears early and re-anchors as the ETA firms up
    (#90 §6 (c)).

Routing endpoints mirror the precheck's existing time-to-leave query: the
airport leg endpoint is the airport `name` (falling back to `code`), which
Distance Matrix resolves, and which reads cleanly as the block's calendar
location. The recheck poll later re-routes exactly the stored origin/destination
pair, so both are captured on the block.

Errors degrade per leg, never abort: a byAir or Maps failure for one direction
drops just that block (logged to stderr) and the next cycle retries — one bad
airport lookup never costs the other block or the other flight.

stdlib-only (`datetime`) per `jbaruch/coding-policy: dependency-management`.
"""

from __future__ import annotations

import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from airport_drive import DesiredDriveBlock  # noqa: E402
from airport_drive_inputs import (  # noqa: E402
    AirportContext,
    airport_context,
    arrival_block,
    departure_block,
)
from byair_client import ByAirError  # noqa: E402
from maps_client import MapsError  # noqa: E402

# `computed_status` values that gate each direction. Before the flight leaves,
# the drive TO the departure airport may still be needed; once it is airborne or
# down, the drive HOME from the arrival airport is what matters. A cancelled /
# diverted flight builds neither here (its teardown is a separate concern).
_TO_AIRPORT_STATUSES = frozenset({"scheduled", "check_in_open", "boarding"})
_FROM_AIRPORT_STATUSES = frozenset({"departed", "en_route", "landed"})


def _parse_instant(raw: object) -> datetime | None:
    """Parse a byAir / scheduled time string into a tz-aware datetime, or None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _effective_instant(state: dict, snapshot_key: str, scheduled_key: str) -> datetime | None:
    """The byAir-truth instant for a leg: snapshot actual when present, else scheduled.

    Mirrors `calendar_reconcile._effective_times` — `last_snapshot.<dep|arr>_time`
    is byAir's live value (the ETA in flight, the actual once known); the
    sync-seeded scheduled time is the pre-poll fallback.
    """
    snapshot = state.get("last_snapshot") or {}
    raw = snapshot.get(snapshot_key)
    if raw is None:
        raw = state.get(scheduled_key)
    return _parse_instant(raw)


def _safe_airport_context(byair, airport_id: object) -> AirportContext:
    """Fetch + extract an airport context, or an empty context on any failure.

    An empty context classifies the route international (the safe, over-buffering
    direction) and omits the tz — never raises. A byAir error here for the
    SECONDARY airport of a direction is tolerable; the caller separately requires
    the PRIMARY airport's code before building a block.
    """
    if not isinstance(airport_id, int) or isinstance(airport_id, bool):
        return AirportContext()
    try:
        payload = byair.get_airport(airport_id)
    except (ByAirError, urllib.error.URLError) as exc:
        print(
            f"flight-assist airport-drive: get_airport({airport_id}) failed: {exc}",
            file=sys.stderr,
        )
        return AirportContext()
    return airport_context(payload)


def _route_seconds(maps, *, origin: str, destination: str) -> int | None:
    """Routed drive seconds (traffic-aware when available), or None on failure.

    Returns `in_traffic_seconds` when the provider modelled traffic, else the
    free-flow `duration_seconds` — matching `precheck._maybe_query_travel_time`.
    A Maps or transport error drops just this leg.
    """
    try:
        result = maps.travel_time(origin=origin, destination=destination)
    except (MapsError, urllib.error.URLError) as exc:
        print(f"flight-assist airport-drive: maps route failed: {exc}", file=sys.stderr)
        return None
    return result.in_traffic_seconds or result.duration_seconds


def _airport_endpoint(ctx: AirportContext) -> str | None:
    """The routable / displayable string for an airport leg endpoint."""
    return ctx.name or ctx.code


def _build_departure(
    state: dict,
    *,
    dep_ctx: AirportContext,
    arr_ctx: AirportContext,
    origin: str,
    maps,
    flight_code: str,
    config: dict | None,
) -> DesiredDriveBlock | None:
    """Assemble the drive-TO-departure-airport block, or None if not buildable."""
    if not dep_ctx.code:
        return None  # no airport code → no usable summary; retry next cycle
    dep_instant = _effective_instant(state, "dep_time", "scheduled_dep_time")
    if dep_instant is None:
        return None
    destination = _airport_endpoint(dep_ctx)
    if not destination:
        return None
    seconds = _route_seconds(maps, origin=origin, destination=destination)
    if seconds is None:
        return None
    return departure_block(
        flight_code=flight_code,
        dep_code=dep_ctx.code,
        dep_ctx=dep_ctx,
        arr_ctx=arr_ctx,
        dep_instant=dep_instant,
        origin=origin,
        destination=destination,
        baseline_seconds=seconds,
        config=config,
    )


def _build_arrival(
    state: dict,
    *,
    dep_ctx: AirportContext,
    arr_ctx: AirportContext,
    home_address: str,
    maps,
    config: dict | None,
) -> DesiredDriveBlock | None:
    """Assemble the drive-HOME-from-arrival-airport block, or None if not buildable."""
    if not arr_ctx.code:
        return None
    arr_instant = _effective_instant(state, "arr_time", "scheduled_arr_time")
    if arr_instant is None:
        return None
    origin = _airport_endpoint(arr_ctx)
    if not origin:
        return None
    seconds = _route_seconds(maps, origin=origin, destination=home_address)
    if seconds is None:
        return None
    return arrival_block(
        arr_code=arr_ctx.code,
        dep_ctx=dep_ctx,
        arr_ctx=arr_ctx,
        arr_instant=arr_instant,
        origin=origin,
        destination=home_address,
        baseline_seconds=seconds,
        config=config,
    )


def build_drive_blocks_for_flight(
    state: dict,
    *,
    byair,
    maps,
    origin: str | None,
    home_address: str | None,
    config: dict | None = None,
) -> list[DesiredDriveBlock]:
    """Build the airport drive blocks a flight currently warrants. Returns 0–2.

    Gated on the flight's `computed_status`: a `to_airport` block while it has
    not departed (and an `origin` is resolved), a `from_airport` block once it is
    airborne or down (and a `home_address` is configured). Each direction
    resolves its airport contexts via `byair.get_airport`, routes the leg via
    `maps.travel_time`, and is dropped if its inputs are missing — never raises.

    Args:
        state: the flight's persisted state record.
        byair: a byAir client exposing `get_airport(airport_id)`.
        maps: a Maps client exposing `travel_time(origin=, destination=)`, or
            None when no routing key is configured (then nothing is built).
        origin: the resolved drive origin for the to_airport leg (the live
            location / home), or None — the to_airport block is skipped without it.
        home_address: the drive-home destination for the from_airport leg, or
            None — the from_airport block is skipped without it.
        config: optional `config.json` dict for the clearance overrides.

    Returns:
        The desired blocks, in to_airport-then-from_airport order. Empty when the
        status gates nothing in, a required input is absent, or `maps` is None.
    """
    if maps is None:
        return []
    snapshot = state.get("last_snapshot") or {}
    status = snapshot.get("computed_status") or "scheduled"
    flight_code = state.get("code") or ""

    want_departure = status in _TO_AIRPORT_STATUSES and origin is not None
    want_arrival = status in _FROM_AIRPORT_STATUSES and home_address is not None
    if not want_departure and not want_arrival:
        return []

    dep_ctx = _safe_airport_context(byair, state.get("dep_airport_id"))
    arr_ctx = _safe_airport_context(byair, state.get("arr_airport_id"))

    blocks: list[DesiredDriveBlock] = []
    if want_departure and origin is not None:
        block = _build_departure(
            state,
            dep_ctx=dep_ctx,
            arr_ctx=arr_ctx,
            origin=origin,
            maps=maps,
            flight_code=flight_code,
            config=config,
        )
        if block is not None:
            blocks.append(block)
    if want_arrival and home_address is not None:
        block = _build_arrival(
            state,
            dep_ctx=dep_ctx,
            arr_ctx=arr_ctx,
            home_address=home_address,
            maps=maps,
            config=config,
        )
        if block is not None:
            blocks.append(block)
    return blocks
