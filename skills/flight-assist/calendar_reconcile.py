"""Calendar reconciliation orchestrator for flight-assist (#55).

The deterministic glue that connects the pure planner (`calendar_plan.py`)
to live Google Calendars via Composio. No LLM in the loop: it resolves
calendar IDs, fetches + normalizes the current calendar state, builds the
per-flight planner inputs, runs `plan_reconciliation`, executes the
returned ops through `composio_client`, and writes the resulting event IDs
back into each flight's `calendar_events` ledger.

Responsibility split (the same discipline the planner enforces):

  - `calendar_plan.py`  — pure decisions (what ops converge the calendar).
  - `disposition.py`    — per-flight disposition (needs the wall clock +
                          active-flights membership).
  - `boarding_lead.py`  — boarding-lead policy (aircraft size / TATL-TPAC).
  - this module         — I/O + glue: resolve IDs, fetch, execute, persist.

Calendar grounding (settled in #55):

  - The flight events + the boarding block live on the operator's flight
    calendar ("Flighty Flights" — the byAir calendar in tile terms). Its ID
    is resolved at runtime from the operator-supplied `byair_calendar_name`
    in config and cached as `byair_calendar_id`; never hardcoded in tile
    code per `rules/flight-data-locality.md`.
  - Reclaim writes its travel blocks onto the user's PRIMARY calendar
    interleaved with real meetings — there is no dedicated Reclaim calendar.
    They are content-classified (`calendar_normalize.is_reclaim_travel`) and
    the planner deletes one only inside a same-airport layover gap.

Scope of this slice (PR 3b): reconcile the flights currently in
`active-flights.json`. The tombstone sweep for flights that have dropped
out of active-flights (switched-away teardown + state archival) lands in
the next slice — the planner already emits teardown ops for cancelled /
diverted flights that are still in the index, which this module executes.

The exact `GOOGLECALENDAR_*` *argument* field names are Composio-version-
specific. They are isolated in the "Composio argument adapters" section
below so a live-toolkit correction is a one-spot fix — the same treatment
`composio_client.py` gives its action slugs. Verify them against the live
toolkit when first wiring against the NAS.

stdlib-only (`datetime`) per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from boarding_lead import resolve_boarding_lead_minutes
from calendar_normalize import NormalizeError, normalize_event
from calendar_plan import (
    MANAGED_ADOPTED,
    MANAGED_CREATED,
    plan_reconciliation,
)
from composio_client import ComposioError
from disposition import resolve_disposition
from state import (
    read_active_flights,
    read_config,
    read_flight_state,
    write_config,
    write_flight_state,
)

# Reclaim travel blocks live on the user's primary calendar (#55). Google's
# well-known alias for the authenticated user's primary calendar.
PRIMARY_CALENDAR_ID = "primary"

# Padding around the flights' own span when fetching calendar events. Wide
# enough to capture a Reclaim travel block sitting just outside a leg's
# scheduled window; the planner's same-airport-gap rule bounds which ones
# are actually deletable, so an over-wide fetch only costs a few extra
# events to classify, never a wrong delete.
_FETCH_WINDOW_PAD = timedelta(hours=6)


class ReconcileError(Exception):
    """Raised on a non-recoverable reconcile setup failure.

    Per-op execution failures are NOT this — they are logged and collected
    so one bad op never aborts the cycle (mirrors the precheck's per-flight
    resilience). This is reserved for failures that make the whole run
    meaningless, e.g. the active-flights index being unreadable.
    """


# --- Calendar argument adapters (verify against the live Composio toolkit) ---
#
# Each helper maps to/from the GOOGLECALENDAR_* `arguments` / response shape.
# The response shape (`{"items": [...]}`, an event resource carrying `id`) is
# the Google-native shape the composio_client tests already assume. The
# request-argument field names are the version-specific surface; isolated here.


def _list_calendar_items(client) -> list[dict]:
    """Return the operator's calendar list as `[{id, summary, ...}, ...]`."""
    data = client.list_calendars()
    return _items(data)


def _find_events_args(*, calendar_id: str, time_min: str, time_max: str) -> dict:
    """Arguments for GOOGLECALENDAR_FIND_EVENT over a calendar + time window.

    `single_events` expands recurring events into instances so each carries
    a concrete start/end the planner can compare (a recurring master would
    not). Verify the field names against the live toolkit.
    """
    return {
        "calendar_id": calendar_id,
        "timeMin": time_min,
        "timeMax": time_max,
        "single_events": True,
    }


def _create_event_args(op: dict) -> dict:
    """Arguments for GOOGLECALENDAR_CREATE_EVENT from a planner `create` op.

    The planner's `body` is `{summary, start, end, private_props}`; map it to
    a Google-native event resource under the calendar. Verify field names
    against the live toolkit.
    """
    body = op["body"]
    return {
        "calendar_id": op["calendar_id"],
        "summary": body["summary"],
        "start": {"dateTime": body["start"]},
        "end": {"dateTime": body["end"]},
        "extendedProperties": {"private": body["private_props"]},
    }


def _patch_event_args(op: dict) -> dict:
    """Arguments for GOOGLECALENDAR_PATCH_EVENT from an `update` / `adopt` op.

    `update` carries `{start, end}` (a delta-shift to byAir truth); `adopt`
    carries `{private_props}` (the tag-only patch that claims a byAir event).
    Only the keys present in the op body are sent, so a patch never clobbers
    a field it did not intend to touch.
    """
    body = op["body"]
    args: dict = {"calendar_id": op["calendar_id"], "event_id": op["event_id"]}
    if "start" in body:
        args["start"] = {"dateTime": body["start"]}
    if "end" in body:
        args["end"] = {"dateTime": body["end"]}
    if "private_props" in body:
        args["extendedProperties"] = {"private": body["private_props"]}
    return args


def _delete_event_args(op: dict) -> dict:
    """Arguments for GOOGLECALENDAR_DELETE_EVENT from a `delete` op."""
    return {"calendar_id": op["calendar_id"], "event_id": op["event_id"]}


def _items(data: dict) -> list[dict]:
    """Pull the `items` list out of a Composio GoogleCalendar list/find response.

    Composio returns the Google-native `{"items": [...]}` payload as the
    action's `data`; some toolkit versions nest it one level under
    `response_data`. Tolerate both, default to empty.
    """
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if items is None:
        nested = data.get("response_data")
        items = nested.get("items") if isinstance(nested, dict) else None
    return items if isinstance(items, list) else []


def _created_event_id(data: dict) -> str | None:
    """Extract the new event's `id` from a CREATE_EVENT response."""
    if not isinstance(data, dict):
        return None
    event_id = data.get("id")
    if event_id:
        return event_id
    nested = data.get("response_data")
    if isinstance(nested, dict):
        return nested.get("id")
    return None


# --- Calendar-ID resolution -------------------------------------------------


def _match_calendar(items: list[dict], name: str) -> str | None:
    """Return the id of the calendar whose summary equals `name`.

    Case-insensitive, whitespace-trimmed exact match on the calendar's
    display name (`summary`). Returns None when no calendar matches, so the
    caller can no-op rather than guess.
    """
    target = name.strip().casefold()
    for item in items:
        summary = (item.get("summary") or "").strip().casefold()
        if summary == target and item.get("id"):
            return item["id"]
    return None


def resolve_byair_calendar_id(client, config: dict) -> str | None:
    """Resolve the flight ("Flighty Flights") calendar ID, caching the result.

    Order:
      1. `config["byair_calendar_id"]` — already cached, use directly.
      2. `config["byair_calendar_name"]` — list calendars, match the name
         once, cache the resolved id back into config.json so later cycles
         skip the lookup.
      3. Neither configured / no match — return None; the caller no-ops
         (there is no flight calendar to reconcile against).

    Never writes "Flighty" into tile code — the name is operator-supplied
    config data per `rules/flight-data-locality.md`.
    """
    cached = config.get("byair_calendar_id")
    if cached:
        return cached
    name = config.get("byair_calendar_name")
    if not name:
        return None
    resolved = _match_calendar(_list_calendar_items(client), name)
    if resolved is None:
        print(
            f"flight-assist reconcile: no calendar named {name!r} found — "
            f"check config.byair_calendar_name against the operator's calendar list",
            file=sys.stderr,
        )
        return None
    # Cache the resolved id. read_config strips schema_version handling; pass
    # only the documented optional fields back to write_config.
    to_persist = {k: v for k, v in config.items() if k != "schema_version"}
    to_persist["byair_calendar_id"] = resolved
    write_config(to_persist)
    return resolved


# --- Per-flight planner-input building ---------------------------------------


def _effective_times(state: dict) -> tuple[str, str]:
    """Return (dep, arr) byAir-truth instants: actual when known, else scheduled.

    The planner converges every managed event to byAir truth, which is the
    actual `last_snapshot.dep_time` / `arr_time` once byAir has published
    them, falling back to the scheduled times before the first poll.
    `is not None`, not truthiness — a present-but-empty actual time is
    malformed and must surface downstream rather than silently use scheduled.
    """
    snapshot = state.get("last_snapshot") or {}
    dep = snapshot.get("dep_time")
    arr = snapshot.get("arr_time")
    return (
        dep if dep is not None else state["scheduled_dep_time"],
        arr if arr is not None else state["scheduled_arr_time"],
    )


def _resolve_lead(state: dict) -> int:
    """Resolve the boarding-lead minutes for a flight from its snapshot.

    Reads the aircraft model + airport coordinates the lead policy needs out
    of `last_snapshot`; every input is optional and the resolver degrades
    gracefully (widebody by `inbound.aircraft_model`, else the narrowbody
    default) until the precheck stamps the richer fields (see
    `state-schema.md` last_snapshot, #55 runtime facts).
    """
    snapshot = state.get("last_snapshot") or {}
    inbound = snapshot.get("inbound") or {}
    return resolve_boarding_lead_minutes(
        aircraft_model=snapshot.get("aircraft_model"),
        inbound_aircraft_model=inbound.get("aircraft_model"),
        dep_lat=snapshot.get("dep_lat"),
        dep_lon=snapshot.get("dep_lon"),
        arr_lat=snapshot.get("arr_lat"),
        arr_lon=snapshot.get("arr_lon"),
    )


def build_planner_flight(state: dict, *, in_active_flights: bool, now: datetime) -> dict:
    """Build one flight's planner input from its persisted state record.

    Resolves the disposition (wall-clock + membership) and the boarding lead,
    and selects byAir-truth dep/arr times, leaving the pure planner to decide
    the ops. The `calendar_events` ledger is passed through so the planner
    keys updates/deletes off it.
    """
    dep, arr = _effective_times(state)
    return {
        "flight_id": state["flight_id"],
        "code": state["code"],
        "trip_id": state["trip_id"],
        "dep_airport_id": state["dep_airport_id"],
        "arr_airport_id": state["arr_airport_id"],
        "dep_time": dep,
        "arr_time": arr,
        "boarding_lead_minutes": _resolve_lead(state),
        "disposition": resolve_disposition(state, in_active_flights=in_active_flights, now=now),
        "calendar_events": state.get("calendar_events") or {},
    }


# --- Event fetch + normalization ---------------------------------------------


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _fetch_window(flights: list[dict]) -> tuple[str, str]:
    """Compute the [time_min, time_max] event-fetch window across all flights.

    Spans from the earliest boarding-block start (dep − lead) to the latest
    arrival, padded both sides so a Reclaim travel block adjacent to a leg is
    still fetched. Returns RFC 3339 UTC strings.
    """
    starts = [
        _parse_instant(f["dep_time"]) - timedelta(minutes=int(f["boarding_lead_minutes"]))
        for f in flights
    ]
    ends = [_parse_instant(f["arr_time"]) for f in flights]
    lo = min(starts) - _FETCH_WINDOW_PAD
    hi = max(ends) + _FETCH_WINDOW_PAD
    return _to_rfc3339(lo), _to_rfc3339(hi)


def _to_rfc3339(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_timed(event: dict) -> bool:
    """True when the normalized event has concrete timed start AND end.

    All-day events normalize to a bare `YYYY-MM-DD` start (no `T`); they are
    never flights or Reclaim travel blocks, and the planner's instant parse
    would reject them, so they are filtered before planning.
    """
    start = event.get("start")
    end = event.get("end")
    return isinstance(start, str) and "T" in start and isinstance(end, str) and "T" in end


def collect_events(client, *, byair_calendar_id: str, time_min: str, time_max: str) -> list[dict]:
    """Fetch + normalize the events the planner needs across both calendars.

    The flight calendar (`classify_reclaim=False` — its events are byAir
    flight events / boarding blocks) and the primary calendar
    (`classify_reclaim=True` — where Reclaim writes its travel blocks). Bare
    all-day events and events normalization rejects (no `id`) are skipped
    with a diagnostic rather than aborting the cycle.
    """
    events: list[dict] = []
    for calendar_id, classify_reclaim in (
        (byair_calendar_id, False),
        (PRIMARY_CALENDAR_ID, True),
    ):
        raw = client.find_events(
            _find_events_args(calendar_id=calendar_id, time_min=time_min, time_max=time_max)
        )
        for raw_event in _items(raw):
            try:
                normalized = normalize_event(
                    raw_event, calendar_id=calendar_id, classify_reclaim=classify_reclaim
                )
            except NormalizeError as exc:
                print(f"flight-assist reconcile: skipping malformed event: {exc}", file=sys.stderr)
                continue
            if _is_timed(normalized):
                events.append(normalized)
    return events


# --- Op execution + ledger writeback -----------------------------------------


def _ledger_entry(*, event_id: str, calendar_id: str, managed: str, signature: str) -> dict:
    return {
        "event_id": event_id,
        "calendar_id": calendar_id,
        "managed": managed,
        "synced_signature": signature,
    }


def _apply_op_to_ledger(op: dict, ledger: dict, client) -> bool:
    """Execute one planner op and mutate `ledger` in place. Return True if the
    flight's state needs to be written back (ledger changed).

    Raises ComposioError / urllib errors to the caller, which logs and
    collects them so one failed op never aborts the cycle. A delete that
    404s (event already gone) is an idempotent success, handled here.
    """
    kind = op["kind"]
    operation = op["op"]

    if operation == "create":
        data = client.create_event(_create_event_args(op))
        new_id = _created_event_id(data)
        if new_id is None:
            raise ComposioError(f"create for {kind} returned no event id: {data!r}")
        ledger[kind] = _ledger_entry(
            event_id=new_id,
            calendar_id=op["calendar_id"],
            managed=MANAGED_CREATED,
            signature=op["signature"],
        )
        return True

    if operation == "adopt":
        client.patch_event(_patch_event_args(op))
        ledger[kind] = _ledger_entry(
            event_id=op["event_id"],
            calendar_id=op["calendar_id"],
            managed=MANAGED_ADOPTED,
            signature=op["signature"],
        )
        return True

    if operation == "update":
        client.patch_event(_patch_event_args(op))
        entry = ledger.get(kind)
        if entry is not None:
            entry["synced_signature"] = op["signature"]
            return True
        return False

    if operation == "delete":
        try:
            client.delete_event(_delete_event_args(op))
        except ComposioError as exc:
            if exc.status_code != 404:
                raise
            # 404 = already gone; idempotent success, fall through to ledger drop.
        # Reclaim travel blocks are not tracked in the ledger; only drop a
        # boarding / flight entry that the ledger actually holds.
        if kind in ledger:
            del ledger[kind]
            return True
        return False

    if operation == "forget":
        if kind in ledger:
            del ledger[kind]
            return True
        return False

    raise ReconcileError(f"unknown planner op {operation!r}")


# --- Top-level orchestration -------------------------------------------------


def run_reconcile(client, *, now: datetime) -> dict:
    """Run one calendar reconciliation cycle over the active flights.

    Returns a summary dict: the resolved calendar id, the op counts, and any
    per-op failures (collected, not raised — a single failed Composio call
    defers that op to the next cycle without aborting the rest).
    """
    config = read_config() or {}
    byair_calendar_id = resolve_byair_calendar_id(client, config)
    if byair_calendar_id is None:
        return {"status": "no_calendar", "planned": 0, "executed": 0, "failed": []}

    active_ids = read_active_flights()
    states_by_id: dict[int, dict] = {}
    flights: list[dict] = []
    for flight_id in active_ids:
        state = read_flight_state(flight_id)
        if state is None:
            continue
        states_by_id[flight_id] = state
        flights.append(build_planner_flight(state, in_active_flights=True, now=now))

    if not flights:
        return {
            "status": "no_flights",
            "byair_calendar_id": byair_calendar_id,
            "planned": 0,
            "executed": 0,
            "failed": [],
        }

    config_ids = {
        "byair_calendar_id": byair_calendar_id,
        "boarding_calendar_id": byair_calendar_id,
        "reclaim_calendar_id": PRIMARY_CALENDAR_ID,
    }
    time_min, time_max = _fetch_window(flights)
    events = collect_events(
        client, byair_calendar_id=byair_calendar_id, time_min=time_min, time_max=time_max
    )

    ops = plan_reconciliation(flights, events, config_ids)

    executed = 0
    failed: list[dict] = []
    dirty: set[int] = set()
    for op in ops:
        flight_id = op["flight_id"]
        state = states_by_id.get(flight_id)
        if state is None:
            # Reclaim deletes carry the downstream leg's flight_id; that state
            # always exists here (built above), so this guards only against a
            # planner change that emits an op for an untracked flight.
            continue
        ledger = state.setdefault("calendar_events", {})
        try:
            changed = _apply_op_to_ledger(op, ledger, client)
        except (ComposioError, OSError) as exc:
            print(
                f"flight-assist reconcile: op {op['op']}/{op['kind']} for flight "
                f"{flight_id} failed: {exc}",
                file=sys.stderr,
            )
            failed.append({"flight_id": flight_id, "op": op["op"], "kind": op["kind"]})
            continue
        executed += 1
        if changed:
            dirty.add(flight_id)

    for flight_id in dirty:
        write_flight_state(states_by_id[flight_id])

    return {
        "status": "ok",
        "byair_calendar_id": byair_calendar_id,
        "planned": len(ops),
        "executed": executed,
        "failed": failed,
    }
