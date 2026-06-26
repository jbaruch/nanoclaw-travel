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
the offset. A shift past the re-anchor threshold is a recreate-then-delete (create
first so there is never a gap, roll the new one back if the old delete fails so
there is never a duplicate), so every write goes through the timezone-correct
`build_block_args` create path; a
sub-threshold drift (traffic jitter under `_REANCHOR_THRESHOLD`) is left alone so
the block does not thrash the calendar every poll (#90 §7). The fetch window is
anchored on the flight's stable scheduled times so a block created before a delay
is still found (and shifted, not duplicated) however far the flight has moved.

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
from calendar_reconcile import _created_event_id, _find_events_args, _items  # noqa: E402
from composio_client import ComposioError  # noqa: E402
from maps_client import MapsError  # noqa: E402

# A re-routed leave-by that drifts less than this from the block already on the
# calendar is left in place — traffic jitter under it must not rewrite the event
# every poll (#90 §7 "shift only past a ~5-min threshold").
_REANCHOR_THRESHOLD = timedelta(minutes=5)

# Padding on the primary-calendar fetch window. It must cover the distance from
# a flight time to its drive block (max clearance 120 min + the drive, or the
# post-arrival delay + the drive), so that — with the window also anchored on the
# flight's STABLE scheduled times — a block created before a delay is still
# fetched no matter how far the flight has since shifted. Anchoring on scheduled
# times (not just the delayed desired window) is what makes this independent of
# the delay magnitude: a pad sized to the delay would be unbounded.
_FETCH_WINDOW_PAD = timedelta(hours=5)

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


def _window(block_or_state) -> tuple[datetime, datetime]:
    """The `(start, end)` instants a block occupies, for a desired block or a
    parsed `BlockState`. Direction-specific: to_airport runs `[anchor − drive,
    anchor]`, from_airport runs `[anchor, anchor + drive]`.

    For a desired block the endpoints are read straight off it; for a parsed
    `BlockState` they are recomputed from its stored `anchor` + `baseline_seconds`
    (what it carries in its description), so the comparison never depends on how
    the calendar API echoes the offset.
    """
    if isinstance(block_or_state, DesiredDriveBlock):
        end = block_or_state.leg_end
        return block_or_state.leg_start, end if end is not None else block_or_state.anchor
    if block_or_state.direction == "from_airport":
        return block_or_state.anchor, block_or_state.anchor + timedelta(
            seconds=block_or_state.baseline_seconds
        )
    return (
        block_or_state.anchor - timedelta(seconds=block_or_state.baseline_seconds),
        block_or_state.anchor,
    )


def _block_window_signature(state) -> str:
    """The `<start>/<end>` signature of a block on the calendar, from its OWN state.

    Byte-stable against the same arithmetic `DesiredDriveBlock.signature()` uses
    (see `_window`), so a no-op compares equal regardless of the calendar API's
    offset formatting.
    """
    start, end = _window(state)
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


def _window_drift(desired: DesiredDriveBlock, existing) -> timedelta:
    """The larger of the start- and end-instant drift between a desired block and
    the block already on the calendar.

    Comparing BOTH endpoints is load-bearing: for a `from_airport` block the start
    is the (stable) arrival anchor while the end carries the routed drive
    duration, so a start-only comparison would read a 20-min → 2-h drive-home
    change as zero drift and never shift it.
    """
    d_start, d_end = _window(desired)
    e_start, e_end = _window(existing)
    return max(abs(d_start - e_start), abs(d_end - e_end))


def _fetch_window(states: list[dict], blocks: list[DesiredDriveBlock]) -> tuple[str, str]:
    """RFC 3339 [time_min, time_max] covering every desired block AND every
    flight's scheduled times, padded.

    Anchoring on the scheduled `dep`/`arr` instants (not only the delayed desired
    window) keeps a pre-delay block in range no matter how far the flight has
    shifted: the stale block sits within `_FETCH_WINDOW_PAD` of the scheduled
    time, so it is fetched and shifted/recreated rather than duplicated.
    """
    instants: list[datetime] = []
    for state in states:
        for key in ("scheduled_dep_time", "scheduled_arr_time"):
            dt = _parse_instant(state.get(key))
            if dt is not None:
                instants.append(dt)
    for block in blocks:
        instants.append(block.leg_start)
        instants.append(block.leg_end if block.leg_end is not None else block.anchor)
    lo = (min(instants) - _FETCH_WINDOW_PAD).astimezone(timezone.utc)
    hi = (max(instants) + _FETCH_WINDOW_PAD).astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return lo.strftime(fmt), hi.strftime(fmt)


def _fetch_block_events(
    composio, *, calendar_id: str, states: list[dict], blocks: list[DesiredDriveBlock]
) -> list[dict]:
    """Fetch the primary-calendar events in the window spanning the desired blocks
    and the flights' scheduled times (see `_fetch_window`).

    Returns the raw Composio events (description intact, so `parse_block` reads
    the `<!--fadrive:-->` state), each annotated with its state-derived signature.
    """
    time_min, time_max = _fetch_window(states, blocks)
    raw = composio.find_events(
        _find_events_args(calendar_id=calendar_id, time_min=time_min, time_max=time_max)
    )
    return _annotate_signatures(_items(raw))


def _execute_op(op: dict, *, composio) -> None:
    """Execute one planner op against the calendar. create / update only.

    A shift (`update`) is a recreate-then-delete, so the replacement always goes
    through `build_block_args`' timezone-aware create path rather than a PATCH
    that would re-introduce the offset-as-UTC ambiguity (#83). Create FIRST so the
    old block is never removed before its replacement exists (no gap on a
    transient create failure — that raises before any delete, leaving the old
    block intact for the next cycle). Then delete the old block; if that delete
    fails for a real reason, roll back the just-created replacement so the cycle
    never leaves a duplicate, and re-raise so the op defers. A delete that 404s
    (old already gone) leaves the new block standing alone — an idempotent success.
    """
    if op["op"] == "create":
        composio.create_event(op["create_args"])
        return
    if op["op"] == "update":
        created = composio.create_event(op["create_args"])
        try:
            composio.delete_event({"calendar_id": op["calendar_id"], "event_id": op["event_id"]})
        except ComposioError as exc:
            if exc.status_code == 404:
                return  # old already gone — the replacement stands alone
            new_id = _created_event_id(created)
            if new_id is not None:
                composio.delete_event({"calendar_id": op["calendar_id"], "event_id": new_id})
            raise
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

    Per-op (create / delete) failures are collected, not raised — one bad write
    defers that op to the next cycle. The one-shot calendar FETCH is the
    exception: a `find_events` failure propagates (there is nothing to reconcile
    against without it), matching `calendar_reconcile`. Returns a summary
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
        composio,
        calendar_id=calendar_id,
        states=[state for state, _ in desired],
        blocks=[block for _, block in desired],
    )

    planned = 0
    executed = 0
    suppressed = 0
    failed: list[dict] = []
    for state, block in desired:
        flight_id = state["flight_id"]
        flight_code = state.get("code") or ""
        existing = _existing_block(events, flight_id, block.direction)
        # Anti-thrash: an existing block whose full window (start AND end) is
        # within the threshold of the freshly-routed one stays put — skip before
        # planning a shift.
        if existing is not None and _window_drift(block, existing) < _REANCHOR_THRESHOLD:
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
            except (ComposioError, urllib.error.URLError) as exc:
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
