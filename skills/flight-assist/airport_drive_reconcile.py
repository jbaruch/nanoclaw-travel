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

`run_airport_drive_reconcile` is the orchestration on top: for each active
flight it assembles the desired blocks, fetches the primary calendar once across
the spanning window, runs `plan_drive_block` per block, and executes the create /
shift ops via Composio. Calendar-as-state, no ledger — an existing block is found
by its marker and its no-op `signature` is derived from the block's OWN stored
state (the `anchor` + baseline it carries), not from Google's start/end echo, so
the comparison is round-trip-stable regardless of how the calendar API formats
the offset. A shift past the re-anchor threshold is a delete + recreate, so every
write goes through the timezone-correct `build_block_args` create path; a
sub-threshold drift (traffic jitter under `_REANCHOR_THRESHOLD`) is left alone so
the block does not thrash the calendar every poll (#90 §7).

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from airport_block import parse_block  # noqa: E402
from airport_drive import DesiredDriveBlock, plan_drive_block  # noqa: E402
from airport_drive_inputs import (  # noqa: E402
    AirportContext,
    airport_context,
    arrival_block,
    departure_block,
)
from byair_client import ByAirError  # noqa: E402

# Reuse the live-verified Composio event-fetch shaping from calendar_reconcile
# (the v3 response nesting in `_items` was verified against the NAS) rather than
# duplicate it here — same skill bundle, no circular import (calendar_reconcile
# does not import this module).
from calendar_reconcile import _find_events_args, _items  # noqa: E402
from composio_client import ComposioError  # noqa: E402
from maps_client import MapsError  # noqa: E402

# A re-routed leave-by that drifts less than this from the block already on the
# calendar is left in place — traffic jitter under it must not rewrite the event
# every poll (#90 §7 "shift only past a ~5-min threshold").
_REANCHOR_THRESHOLD = timedelta(minutes=5)

# Padding on the primary-calendar fetch window, so a block whose endpoints sit
# just outside the spanning [min leg_start, max leg_end] is still fetched.
_FETCH_WINDOW_PAD = timedelta(hours=1)

# Where the airport drive blocks live (#90 decision: the primary calendar,
# alongside drive-planner's `Drive:` meeting blocks).
PRIMARY_CALENDAR_ID = "primary"

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
    """The byAir-truth instant for a leg: snapshot actual when usable, else scheduled.

    `last_snapshot.<dep|arr>_time` is byAir's live value (the ETA in flight, the
    actual once known); the sync-seeded scheduled time is the fallback. Like
    `calendar_reconcile._effective_times` it prefers the snapshot actual — but it
    deliberately diverges on a present-but-unparseable snapshot value (e.g. an
    empty string from bad byAir data): `_effective_times` would use it as-is,
    whereas this falls back to scheduled rather than suppressing the block, and
    logs the bad value (silent bad data contradicts the per-leg degradation
    posture). Returns None only when scheduled is unusable too — the caller logs
    that drop.
    """
    snapshot = state.get("last_snapshot") or {}
    raw = snapshot.get(snapshot_key)
    instant = _parse_instant(raw)
    if instant is not None:
        return instant
    if raw is not None:
        print(
            f"flight-assist airport-drive: unparseable {snapshot_key}={raw!r}; "
            f"falling back to {scheduled_key}",
            file=sys.stderr,
        )
    return _parse_instant(state.get(scheduled_key))


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
    # `is not None`, not truthiness — a modelled 0-second in-traffic estimate is a
    # valid time and must not fall back to free-flow.
    if result.in_traffic_seconds is not None:
        return result.in_traffic_seconds
    return result.duration_seconds


def _clean_endpoint(value: str | None) -> str | None:
    """A stripped, non-empty endpoint string, or None when absent / blank.

    A resolved `origin` / `home_address` can arrive as `""` or whitespace (an
    empty config value, an origin ladder that produced nothing usable). The real
    `MapsClient.travel_time` raises `ValueError` on a blank endpoint — which
    `_route_seconds` does not catch — so a blank string must read as absent here
    (the leg is skipped) rather than enter routing and abort the assembler.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


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
        print(
            f"flight-assist airport-drive: no usable departure time for "
            f"{flight_code or state.get('flight_id')}; dropping to_airport block",
            file=sys.stderr,
        )
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
        print(
            f"flight-assist airport-drive: no usable arrival time for "
            f"{state.get('code') or state.get('flight_id')}; dropping from_airport block",
            file=sys.stderr,
        )
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
        byair: a byAir client exposing `get_airport(airport_id)`, or None — then
            nothing is built (preserves the never-raises contract).
        maps: a Maps client exposing `travel_time(origin=, destination=)`, or
            None when no routing key is configured (then nothing is built).
        origin: the resolved drive origin for the to_airport leg (the live
            location / home), or None — the to_airport block is skipped when it is
            None, empty, or whitespace.
        home_address: the drive-home destination for the from_airport leg, or
            None — the from_airport block is skipped when it is None, empty, or
            whitespace.
        config: optional `config.json` dict for the clearance overrides.

    Returns:
        The desired blocks, in to_airport-then-from_airport order. Empty when the
        status gates nothing in, a required input is absent, or `maps` is None.
    """
    # Either client absent → build nothing. maps is legitimately None when no
    # routing key is configured; byair guards the never-raises contract against a
    # caller that fumbles the client (it would AttributeError in the airport lookup).
    if maps is None or byair is None:
        return []
    snapshot = state.get("last_snapshot") or {}
    status = snapshot.get("computed_status") or "scheduled"
    flight_code = state.get("code") or ""

    # Normalize endpoints before gating: a blank origin / home_address is absent,
    # not a routable string (see `_clean_endpoint`).
    origin = _clean_endpoint(origin)
    home_address = _clean_endpoint(home_address)
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


# --- Orchestration: fetch the calendar, plan, execute ------------------------


def _block_window_signature(state) -> str:
    """The `<start>/<end>` signature of a block on the calendar, from its OWN state.

    Computed from the block's stored `anchor` + `baseline_seconds` (which it
    carries in its description), NOT from the calendar event's start/end — so it
    is byte-stable against the same arithmetic `DesiredDriveBlock.signature()`
    uses, regardless of how the calendar API echoes the offset. Mirrors the
    direction-specific window math: to_airport runs `[anchor − drive, anchor]`,
    from_airport runs `[anchor, anchor + drive]`.
    """
    if state.direction == "from_airport":
        start = state.anchor
        end = state.anchor + timedelta(seconds=state.baseline_seconds)
    else:
        start = state.anchor - timedelta(seconds=state.baseline_seconds)
        end = state.anchor
    return f"{start.isoformat()}/{end.isoformat()}"


def _annotate_signatures(events: list[dict]) -> list[dict]:
    """Attach the state-derived `signature` to each event that is one of our blocks.

    `plan_drive_block` compares `event["signature"]` to the desired signature to
    decide no-op vs shift. A non-block event (no `<!--fadrive:-->` state) gets no
    signature and is ignored by the planner's marker scan.
    """
    for event in events:
        state = parse_block(event)
        if state is not None:
            event["signature"] = _block_window_signature(state)
    return events


def _existing_block(events: list[dict], flight_id, direction: str):
    """The parsed `BlockState` for this flight+direction among events, or None."""
    target = str(flight_id)
    for event in events:
        state = parse_block(event)
        if state is not None and state.flight_id == target and state.direction == direction:
            return state
    return None


def _leave_by(block_or_state) -> datetime:
    """The leave-by instant a block defends — `leg_start` for a desired block,
    `baseline_leave_by` for a parsed `BlockState` (both = anchor for from_airport)."""
    if isinstance(block_or_state, DesiredDriveBlock):
        return block_or_state.leg_start
    return block_or_state.baseline_leave_by


def _fetch_window(blocks: list[DesiredDriveBlock]) -> tuple[str, str]:
    """RFC 3339 [time_min, time_max] spanning all desired blocks, padded."""
    starts = [b.leg_start for b in blocks]
    ends = [b.leg_end if b.leg_end is not None else b.anchor for b in blocks]
    lo = (min(starts) - _FETCH_WINDOW_PAD).astimezone(timezone.utc)
    hi = (max(ends) + _FETCH_WINDOW_PAD).astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return lo.strftime(fmt), hi.strftime(fmt)


def _fetch_block_events(
    composio, *, calendar_id: str, blocks: list[DesiredDriveBlock]
) -> list[dict]:
    """Fetch the primary-calendar events in the window spanning the desired blocks.

    Returns the raw Composio events (description intact, so `parse_block` reads
    the `<!--fadrive:-->` state), each annotated with its state-derived signature.
    """
    time_min, time_max = _fetch_window(blocks)
    raw = composio.find_events(
        _find_events_args(calendar_id=calendar_id, time_min=time_min, time_max=time_max)
    )
    return _annotate_signatures(_items(raw))


def _execute_op(op: dict, *, composio) -> None:
    """Execute one planner op against the calendar. create / update only.

    A shift (`update`) is a delete + recreate, so the recreated block always goes
    through `build_block_args`' timezone-aware create path rather than a PATCH
    that would re-introduce the offset-as-UTC ambiguity (#83). A delete that 404s
    (already gone) is an idempotent success.
    """
    if op["op"] == "create":
        composio.create_event(op["create_args"])
        return
    if op["op"] == "update":
        try:
            composio.delete_event({"calendar_id": op["calendar_id"], "event_id": op["event_id"]})
        except ComposioError as exc:
            if exc.status_code != 404:
                raise
        composio.create_event(op["create_args"])
        return
    raise ValueError(f"airport_drive_reconcile: unexpected op {op['op']!r}")


def run_airport_drive_reconcile(
    states: list[dict],
    *,
    composio,
    byair,
    maps,
    origin: str | None,
    home_address: str | None,
    calendar_id: str = PRIMARY_CALENDAR_ID,
    config: dict | None = None,
) -> dict:
    """Reconcile every active flight's airport drive blocks against the calendar.

    For each state in `states`, assembles the blocks it currently warrants
    (`build_drive_blocks_for_flight`), then — across all of them — fetches the
    block calendar once over the spanning window, and per block runs
    `plan_drive_block` and executes the resulting create / shift. A shift whose
    re-routed leave-by drifts less than `_REANCHOR_THRESHOLD` from the block
    already on the calendar is suppressed (anti-thrash, #90 §7).

    Per-flight / per-op failures are collected, never raised — one bad Composio
    call defers that op to the next cycle. Returns a summary
    `{status, planned, executed, suppressed, failed}`.

    Args:
        states: the active flights' state records.
        composio: a Composio client (`find_events` / `create_event` /
            `delete_event`).
        byair / maps: the airport-context and routing clients (see
            `build_drive_blocks_for_flight`); either None builds nothing.
        origin: the resolved to_airport drive origin (live location / home).
        home_address: the from_airport drive destination.
        calendar_id: the calendar the blocks live on (primary).
        config: optional `config.json` for the clearance overrides.
    """
    desired: list[tuple[dict, DesiredDriveBlock]] = []
    for state in states:
        for block in build_drive_blocks_for_flight(
            state, byair=byair, maps=maps, origin=origin, home_address=home_address, config=config
        ):
            desired.append((state, block))

    if not desired:
        return {"status": "ok", "planned": 0, "executed": 0, "suppressed": 0, "failed": []}

    events = _fetch_block_events(
        composio, calendar_id=calendar_id, blocks=[block for _, block in desired]
    )

    planned = 0
    executed = 0
    suppressed = 0
    failed: list[dict] = []
    for state, block in desired:
        flight_id = state["flight_id"]
        flight_code = state.get("code") or ""
        existing = _existing_block(events, flight_id, block.direction)
        # Anti-thrash: an existing block whose leave-by is within the threshold of
        # the freshly-routed one stays put — skip before planning a shift.
        if existing is not None:
            drift = abs(_leave_by(block) - _leave_by(existing))
            if drift < _REANCHOR_THRESHOLD:
                suppressed += 1
                continue
        ops = plan_drive_block(
            flight_id=flight_id,
            flight_code=flight_code,
            desired=block,
            events=events,
            calendar_id=calendar_id,
        )
        for op in ops:
            planned += 1
            try:
                _execute_op(op, composio=composio)
            except (ComposioError, OSError) as exc:
                print(
                    f"flight-assist airport-drive: op {op['op']}/{op['kind']} for flight "
                    f"{flight_id} failed: {exc}",
                    file=sys.stderr,
                )
                failed.append({"flight_id": flight_id, "op": op["op"], "kind": op["kind"]})
                continue
            executed += 1
    return {
        "status": "ok",
        "planned": planned,
        "executed": executed,
        "suppressed": suppressed,
        "failed": failed,
    }
