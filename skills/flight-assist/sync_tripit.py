#!/usr/bin/env python3
"""Daily sync of tracked flights from byAir into the active-flights index.

The precheck polls flights in `active-flights.json`. This script
refreshes that index from byAir's `list_trips` so newly-tracked
flights start getting polled and expired/removed flights stop.

Scheduled-task contract (same as precheck.py): emits a single-line
JSON payload `{"wake_agent": <bool>, "data": {...}}`. wake_agent is
true when the diff produced added/removed flights worth telling the
agent about; false otherwise. Per `coding-policy: script-delegation`
"Precheck Gating".

Uses the outer-boundary-process-contract carve-out for unexpected
exceptions per `coding-policy: error-handling`.

Run cadence: daily at ~04:00 local. The precheck script also calls
into sync logic opportunistically when it encounters a flight_id
not in state — both paths share `_reconcile_active_flights()`.

stdlib-only: `json`, `sys`, `traceback` per `coding-policy:
dependency-management`.
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BUNDLE_DIR))

from byair_client import ByAirClient, ByAirError  # noqa: E402
from state import (  # noqa: E402
    delete_flight_state,
    read_active_flights,
    read_flight_state,
    write_active_flights,
    write_flight_state,
)


def main() -> int:
    # outer-boundary-process-contract: same shape as precheck.py.
    # The scheduled task reads non-zero exit OR invalid stdout as
    # "skip waking the agent". A bare programming bug bubbling out
    # would silently disable the wake contract for every subsequent
    # run; catch it here, emit safe-shape JSON, return 0.
    try:
        diff = _run_sync(now_utc=datetime.now(timezone.utc))
        _emit_diff(diff)
        return 0
    except Exception:  # noqa: BLE001 — outer-boundary-process-contract
        traceback.print_exc(file=sys.stderr)
        _emit_diff({"added": [], "removed": [], "error": "sync_exception"})
        return 0


def _emit_diff(diff: dict) -> None:
    """Write the sync-contract JSON to stdout (single line).

    Wraps the added/removed lists into the same `data.events` shape
    the precheck script emits, so SKILL.md Step 3's composition
    table is the single consumer contract — no separate sync-wake
    payload to document. Per `coding-policy: script-delegation`
    "Precheck Gating": data must carry the inputs the agent needs.
    """
    events = []
    for entry in diff.get("added", []) or []:
        events.append(
            {
                "flight_id": entry["flight_id"],
                "event": {
                    "reason": "tracked_flight_added",
                    "code": entry.get("code"),
                    "scheduled_dep_time": entry.get("scheduled_dep_time"),
                    "scheduled_arr_time": entry.get("scheduled_arr_time"),
                },
            }
        )
    for entry in diff.get("removed", []) or []:
        events.append(
            {
                "flight_id": entry["flight_id"],
                "event": {
                    "reason": "tracked_flight_removed",
                    "code": entry.get("code"),
                    "scheduled_dep_time": entry.get("scheduled_dep_time"),
                    "scheduled_arr_time": entry.get("scheduled_arr_time"),
                },
            }
        )
    has_events = bool(events)
    payload = {"wake_agent": has_events, "data": {"events": events}}
    if "error" in diff:
        payload["data"]["error"] = diff["error"]
    print(json.dumps(payload, separators=(",", ":")))


def _run_sync(*, now_utc: datetime) -> dict:
    """Execute one sync pass, returning the {added, removed} diff."""
    byair = ByAirClient.from_env()
    try:
        # ownership="mine" so friends' tracked trips never enter the
        # active-flights index. byAir's default is "all", which pulled in
        # friends' flights and surfaced [M] wake events (delay, gate,
        # boarding) the operator can't act on. The request-side filter is
        # authoritative; the per-flight `ownership` field in the response
        # is unreliable (defaults to "mine" when omitted).
        trips_payload = byair.list_trips(status="active", ownership="mine")
    except urllib.error.URLError as transport_err:
        # Transport failure: skip this sync pass. The next scheduled
        # run will retry. Per `coding-policy: error-handling`
        # "Specific Exceptions" + "Graceful Fallback".
        print(
            f"flight-assist sync: transport error: {transport_err}",
            file=sys.stderr,
        )
        return {"added": [], "removed": [], "error": "transport_error"}
    except ByAirError as byair_err:
        print(
            f"flight-assist sync: byair error: {byair_err}",
            file=sys.stderr,
        )
        return {"added": [], "removed": [], "error": byair_err.error_type}

    upstream_flights = _extract_flights(trips_payload)
    return _reconcile_active_flights(upstream_flights, now_utc=now_utc)


def _extract_flights(trips_payload: dict) -> list[dict]:
    """Pull every flight dict out of the trips response into a flat list."""
    flights: list[dict] = []
    for trip in trips_payload.get("trips", []) or []:
        for flight in trip.get("flights", []) or []:
            # Carry the trip_id into the flight dict so the writer has it.
            flight = {**flight, "_trip_id": trip.get("id")}
            flights.append(flight)
    return flights


def _has_calendar_ledger(state: dict | None) -> bool:
    """True when a state record still carries a non-empty `calendar_events` map.

    A non-empty ledger means flight-assist has managed calendar events
    (boarding block and/or adopted byAir flight event) that the reconcile
    sweep must tear down before the state file can be dropped. Absent, `{}`,
    or a missing record all read as "nothing to tear down".
    """
    return bool(state and state.get("calendar_events"))


def _reconcile_active_flights(upstream_flights: list[dict], *, now_utc: datetime) -> dict:
    """Diff the upstream list against the on-disk active-flights index.

    Returns `{added: [flight_id, ...], removed: [flight_id, ...]}`.

    For each `added` flight, writes an initial per-flight state record
    so the next precheck cycle has scheduled times to work with.
    For each `removed` flight, deletes its state file — UNLESS the record
    still carries a `calendar_events` ledger, in which case the file is
    retained as a teardown tombstone (see `_has_calendar_ledger`).
    """
    upstream_ids = {
        flight["id"] for flight in upstream_flights if isinstance(flight.get("id"), int)
    }
    current_ids = set(read_active_flights())

    added_ids = upstream_ids - current_ids
    removed_ids = current_ids - upstream_ids

    upstream_by_id = {
        flight["id"]: flight for flight in upstream_flights if isinstance(flight.get("id"), int)
    }

    # Build added entries with the upstream flight metadata.
    added: list[dict] = []
    for flight_id in sorted(added_ids):
        flight = upstream_by_id[flight_id]
        write_flight_state(_initial_state(flight, now_utc=now_utc))
        added.append(
            {
                "flight_id": flight_id,
                "code": flight.get("code"),
                "scheduled_dep_time": flight.get("scheduledDepTime"),
                "scheduled_arr_time": flight.get("scheduledArrTime"),
            }
        )

    # Capture removed-flight metadata BEFORE deleting state (the
    # notification template in SKILL.md needs `code` + scheduled times
    # to compose "Flight <code> stopped tracking...").
    removed: list[dict] = []
    for flight_id in sorted(removed_ids):
        prior = read_flight_state(flight_id)
        removed.append(
            {
                "flight_id": flight_id,
                "code": prior.get("code") if prior else None,
                "scheduled_dep_time": prior.get("scheduled_dep_time") if prior else None,
                "scheduled_arr_time": prior.get("scheduled_arr_time") if prior else None,
            }
        )
        # Retain the state file as a teardown tombstone when it still holds a
        # `calendar_events` ledger (#55). Deleting it here would orphan those
        # managed calendar events forever: byAir won't remove them, and the
        # per-flight wake loop can't see a flight that has left active-flights.
        # The reconcile sweep (calendar_reconcile.run_reconcile) tears the
        # events down off the retained ledger, then archives the file. Drop it
        # immediately only when there is nothing to tear down.
        if _has_calendar_ledger(prior):
            continue
        delete_flight_state(flight_id)

    # Repair codeshare records poisoned before the marketing-code fix (#159):
    # a prior precheck poll (byAir get_flight, operating perspective) may have
    # overwritten `code` with the operating designator (e.g. 9E4908) instead of
    # the marketing one (DL4908). The poll now preserves `code`, but that also
    # preserves an already-wrong value — nothing else heals it. list_trips is
    # the marketing-code authority, so refresh the display code on every
    # retained flight from upstream, repairing corrupted state on the next daily
    # sync. Touch only `code` (and the snapshot's mirror of it); the poll owns
    # every other field.
    repaired: list[dict] = []
    for flight_id in sorted(upstream_ids & current_ids):
        upstream_code = upstream_by_id[flight_id].get("code")
        if not upstream_code:
            continue
        prior = read_flight_state(flight_id)
        if prior is None or prior.get("code") == upstream_code:
            continue
        prior["code"] = upstream_code
        snapshot = prior.get("last_snapshot")
        if isinstance(snapshot, dict):
            snapshot["code"] = upstream_code
        write_flight_state(prior)
        repaired.append({"flight_id": flight_id, "code": upstream_code})

    # Persist the new active-flights index. read_active_flights returns
    # sorted/preserved order; here we sort for determinism.
    write_active_flights(sorted(upstream_ids))

    return {"added": added, "removed": removed, "repaired": repaired}


def _initial_state(flight: dict, *, now_utc: datetime) -> dict:
    """Build the initial state record for a newly-tracked flight.

    All required fields per state-schema.md must be present so
    `write_flight_state`'s validator accepts it. `last_snapshot` is
    None on first write — the next precheck cycle populates it.
    """
    dep_airport = flight.get("depAirport") or {}
    arr_airport = flight.get("arrAirport") or {}
    return {
        "flight_id": flight["id"],
        "code": flight.get("code", ""),
        "ownership": flight.get("ownership", "mine"),
        "trip_id": flight.get("_trip_id") or 0,
        "scheduled_dep_time": flight.get("scheduledDepTime", ""),
        "scheduled_arr_time": flight.get("scheduledArrTime", ""),
        "dep_airport_id": dep_airport.get("id", 0),
        "arr_airport_id": arr_airport.get("id", 0),
        "last_polled_at": now_utc.isoformat().replace("+00:00", "Z"),
        "last_snapshot": None,
        "phase_markers": {
            "day_before_fired": False,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": False,
        },
        "last_wake_at": None,
        "last_wake_reason": None,
    }


# Public entry point for the precheck to call when it discovers a
# flight_id not on the active-flights index. The precheck doesn't
# need the full sync — just the per-flight state initialization.
def initialize_flight_from_byair(*, flight: dict, now_utc: datetime) -> None:
    """Write the initial state record for a flight first seen via the precheck.

    Tolerates either `id` (byair_get_flight raw shape) or `flight_id`
    (precheck's internal shape) as the integer identifier. Normalizes
    to `id` before calling `_initial_state` so the downstream lookup
    doesn't KeyError on flight_id-only payloads.
    """
    flight_id = flight.get("id")
    if not isinstance(flight_id, int) or isinstance(flight_id, bool):
        flight_id = flight.get("flight_id")
        if not isinstance(flight_id, int) or isinstance(flight_id, bool):
            return
    normalized = {**flight, "id": flight_id}
    write_flight_state(_initial_state(normalized, now_utc=now_utc))


# Provide read_flight_state as a re-export so callers can do the
# "is this already known?" check without importing state directly.
__all__ = [
    "initialize_flight_from_byair",
    "read_flight_state",
]


if __name__ == "__main__":
    sys.exit(main())
