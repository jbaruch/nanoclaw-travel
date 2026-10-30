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

Invocation: the `sync-tripit` skill's precheck delegates to this script
as a subprocess when its adaptive gate passes (cadence `*/5 * * * *`,
gated on an imminent flight or a stale index). The flight-assist precheck
also calls into sync logic opportunistically when it encounters a
flight_id not in state — both paths share `_reconcile_active_flights()`.

stdlib-only: `json`, `sys`, `traceback` per `coding-policy:
dependency-management`.
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BUNDLE_DIR))

from byair_client import _MALFORMED_REMEDY, ByAirClient, ByAirError  # noqa: E402
from disposition import DispositionError, _effective_arrival  # noqa: E402
from state import (  # noqa: E402
    SYNC_HEALTH_FILE,
    StateError,
    delete_flight_state,
    read_active_flights,
    read_flight_state,
    read_sync_health,
    state_dir,
    write_active_flights,
    write_flight_state,
    write_sync_health,
)

# Per-call timeout for the byAir HTTP client below. `ByAirClient`'s own
# default is 30s, and this script runs as a subprocess inside sync-tripit's
# precheck budget (`_SYNC_SUBPROCESS_TIMEOUT` in
# `skills/sync-tripit/precheck.py`, derived from the 90s kill that skill
# declares). A call left at the client default would spend a third of that
# budget on one hung upstream.
#
# 20s fails fast into the `urllib.error.URLError` transient-transport
# branch in `_run_sync`, which skips the pass and lets the next 5-min fire
# retry — a graceful, diagnosable outcome. Riding the outer subprocess
# timeout instead loses the diff entirely and emits a bare
# `sync_subprocess_timeout`. Bounding it here, at the construction site,
# keeps the bound true for every caller of this script rather than only
# the precheck path. Per #212; same inner-vs-outer collision as #28.
_BYAIR_CALL_TIMEOUT_SECONDS = 20.0

# Consecutive failed sync passes before the outage alert fires (#326). On a
# flight day the gate syncs every 5 min, so 3 is 15 min of silence; between
# trips the gate syncs about every 6 h. One alert per failure run, plus one
# recovery notice when a pass succeeds after an alert.
_OUTAGE_ALERT_THRESHOLD = 3


def main() -> int:
    try:
        diff = _run_sync(now_utc=datetime.now(UTC))
        _attach_sync_health(diff, internal_error=None)
        _emit_diff(diff)
        _commit_sync_health(diff)
        return 0
    # outer-boundary-process-contract: the scheduled task reads a non-zero
    # exit OR invalid stdout as "skip waking the agent", silently. This
    # handler emits the safe-shape `sync_exception` JSON (plus the internal
    # failure count) and exits 0; letting the exception propagate would
    # break that stdout contract and disable every later wake decision.
    except Exception as exc:  # noqa: BLE001 — outer-boundary-process-contract
        traceback.print_exc(file=sys.stderr)
        diff = {"added": [], "removed": [], "error": "sync_exception"}
        # A local fault (config, state, bug) is not a byAir outage: it runs
        # its own counter and alert, and leaves the byAir counter alone (#326).
        try:
            _attach_sync_health(diff, internal_error=type(exc).__name__)
        # outer-boundary-process-contract: the same caller reads a non-zero
        # exit or invalid stdout as "skip waking the agent", silently. A
        # defect in the health bookkeeping is printed and reported as
        # `health_error`; letting it escape this handler would skip
        # `_emit_diff` and break that stdout contract.
        except Exception as health_exc:  # noqa: BLE001 — outer-boundary-process-contract
            traceback.print_exc(file=sys.stderr)
            diff["health_error"] = type(health_exc).__name__
        _emit_diff(diff)
        _commit_sync_health(diff)
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
    for source_event in diff.get("source_events", []) or []:
        # Source-level events concern byAir itself, not one flight.
        events.append({"flight_id": None, "event": source_event})
    has_events = bool(events)
    payload = {"wake_agent": has_events, "data": {"events": events}}
    if "error" in diff:
        payload["data"]["error"] = diff["error"]
    if "health_error" in diff:
        payload["data"]["health_error"] = diff["health_error"]
    # Flushed here so a closed stdout fails before `_commit_sync_health`
    # records the events as sent.
    print(json.dumps(payload, separators=(",", ":")), flush=True)


_HEALTH_REMEDY = (
    "check that file's permissions and the disk's free space; if it is from a newer "
    "flight-assist, upgrade nanoclaw-travel (never delete it: it holds the alert gate); "
    "only a corrupt file may be removed to restart the counts"
)


def _attach_sync_health(diff: dict, *, internal_error: str | None) -> None:
    """Plan this pass's sync-health transition; attach the resulting events.

    The counters are bookkeeping beside the sync, never a gate on it: by now
    the active-flights index may already hold this pass's changes, so a
    failed health read must not cost the diff its added/removed events. The
    failure is reported on stderr and as `health_error` in the payload
    instead. The new record waits in the diff for `_commit_sync_health`,
    which runs only after the events are on stdout.
    """
    byair_error = None if internal_error is not None else diff.get("error")
    try:
        diff["source_events"], diff["_health_record"] = _plan_sync_health(
            byair_error=byair_error, internal_error=internal_error
        )
    except (StateError, OSError, ValueError) as health_err:
        print(
            f"flight-assist sync: health counter in {state_dir() / SYNC_HEALTH_FILE} not read "
            f"({health_err}) — {_HEALTH_REMEDY}",
            file=sys.stderr,
        )
        diff["health_error"] = type(health_err).__name__


def _commit_sync_health(diff: dict) -> None:
    """Persist the planned record once its events have been emitted.

    `alerted` means the alert went out, so a pass that never emitted leaves
    the gate as it was and the next pass re-derives the same events. A
    failed write after emission repeats them next pass instead: the alerts
    are at-least-once, never lost.
    """
    record = diff.get("_health_record")
    if record is None:
        return
    try:
        write_sync_health(record)
    except (StateError, OSError, ValueError) as health_err:
        print(
            f"flight-assist sync: health counter in {state_dir() / SYNC_HEALTH_FILE} not updated "
            f"({health_err}); this pass's source events will repeat — {_HEALTH_REMEDY}",
            file=sys.stderr,
        )


def _advance(
    record: dict,
    keys: tuple[str, str, str],
    error: str | None,
    reasons: tuple[str, str],
) -> list[dict]:
    """Advance one failure run in `record` in place; return its events.

    A failure increments the count and returns the alert event once the
    count reaches `_OUTAGE_ALERT_THRESHOLD` with no alert sent yet for this
    run. A success resets the run and returns the recovery event when an
    alert had gone out.
    """
    count_key, alerted_key, error_key = keys
    alert_reason, recovered_reason = reasons
    if error is not None:
        record[count_key] += 1
        record[error_key] = error
        if record[count_key] >= _OUTAGE_ALERT_THRESHOLD and not record[alerted_key]:
            record[alerted_key] = True
            return [
                {"reason": alert_reason, "consecutive_failures": record[count_key], "error": error}
            ]
        return []
    events = []
    if record[alerted_key]:
        events.append({"reason": recovered_reason, "failures_before_recovery": record[count_key]})
    record.update({count_key: 0, alerted_key: False, error_key: None})
    return events


def _plan_sync_health(
    *, byair_error: str | None, internal_error: str | None
) -> tuple[list[dict], dict | None]:
    """Advance both failure runs for this pass, without writing.

    Returns the source-level events and the record to persist once they are
    emitted, or None when the record is unchanged.

    A pass that died on a local fault advances the internal run and leaves
    the byAir run untouched (byAir was never asked). A pass that produced a
    result clears the internal run and advances the byAir run by its
    outcome. Before #326 a failed pass looked exactly like a quiet one, and
    897 consecutive failures raised nothing.
    """
    health = read_sync_health()
    record = dict(health)
    events = _advance(
        record,
        ("internal_failures", "internal_alerted", "last_internal_error"),
        internal_error,
        ("sync_internal_error", "sync_internal_recovered"),
    )
    if internal_error is None:
        events += _advance(
            record,
            ("consecutive_failures", "alerted", "last_error"),
            byair_error,
            ("byair_unreachable", "byair_recovered"),
        )
    return events, (record if record != health else None)


def _run_sync(*, now_utc: datetime) -> dict:
    """Execute one sync pass, returning the {added, removed} diff."""
    byair = ByAirClient.from_env(timeout=_BYAIR_CALL_TIMEOUT_SECONDS)
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

    try:
        upstream_flights = _extract_flights(trips_payload)
    except ByAirError as byair_err:
        # A reply byAir sent in the wrong shape is a byAir failure, not a
        # local fault: it counts toward byair_unreachable (#326).
        print(f"flight-assist sync: byair error: {byair_err}", file=sys.stderr)
        return {"added": [], "removed": [], "error": byair_err.error_type}
    return _reconcile_active_flights(upstream_flights, now_utc=now_utc)


def _extract_flights(trips_payload: object) -> list[dict]:
    """Pull every flight dict out of the trips response into a flat list.

    Raises `ByAirError("malformed_response")` unless byAir's reply has the
    documented shape: an explicit `trips` list, each trip an object with an
    explicit `flights` list, each flight an object with an int `id`. A
    missing collection must never read as "no flights", which would make
    reconciliation untrack every active flight.
    """
    trips = trips_payload.get("trips") if isinstance(trips_payload, dict) else None
    if not isinstance(trips, list):
        raise ByAirError(
            "malformed_response", "list_trips reply has no `trips` list" + _MALFORMED_REMEDY
        )
    flights: list[dict] = []
    for trip in trips:
        trip_flights = trip.get("flights") if isinstance(trip, dict) else None
        if not isinstance(trip_flights, list):
            raise ByAirError(
                "malformed_response", "list_trips trip has no `flights` list" + _MALFORMED_REMEDY
            )
        for flight in trip_flights:
            flight_id = flight.get("id") if isinstance(flight, dict) else None
            if not isinstance(flight_id, int) or isinstance(flight_id, bool):
                raise ByAirError(
                    "malformed_response",
                    "list_trips flight has no integer `id`" + _MALFORMED_REMEDY,
                )
            # Carry the trip_id into the flight dict so the writer has it.
            flights.append({**flight, "_trip_id": trip.get("id")})
    return flights


def _has_calendar_ledger(state: dict | None) -> bool:
    """True when a state record still carries a non-empty `calendar_events` map.

    A non-empty ledger means flight-assist has managed calendar events
    (boarding block and/or adopted byAir flight event) that the reconcile
    sweep must tear down before the state file can be dropped. Absent, `{}`,
    or a missing record all read as "nothing to tear down".
    """
    return bool(state and state.get("calendar_events"))


def _completed_removal(prior: dict | None, now_utc: datetime) -> bool:
    """Notification-only expiry; calendar disposition still owns teardown/history.

    Quiet only a provably past zoned arrival; uncertainty stays actionable.
    """
    if prior is None:
        return False
    try:
        return _effective_arrival(prior) < now_utc
    except (DispositionError, OverflowError):
        # A present but invalid actual arrival never falls back to an older
        # schedule. Preserve the removal event without guessing completion.
        return False


def _reconcile_active_flights(upstream_flights: list[dict], *, now_utc: datetime) -> dict:
    """Diff the upstream list against the on-disk active-flights index.

    Returns metadata for added flights and removals not proven completed.
    Every removed ID still leaves the index under the same state lifecycle.

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
    # to compose a check for a future/active or unconfirmed removal).
    # Completed removals stay quiet; their index/state lifecycle is unchanged.
    removed: list[dict] = []
    for flight_id in sorted(removed_ids):
        prior = read_flight_state(flight_id)
        if not _completed_removal(prior, now_utc):
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
