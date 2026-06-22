"""Resolve a flight's reconciliation disposition for the calendar planner.

`plan_reconciliation` (see `calendar_plan.py`) needs a `disposition` per
flight to decide whether to reconcile its managed events normally, tear
them down, or leave them as a historical record. Computing that disposition
needs two things the pure planner deliberately stays out of: the wall clock
and `active-flights.json` membership. This module is the one place that
reads them, so the planner stays a pure function — the same split as
`boarding_lead.py` keeping the volatile lead policy out of the planner.

The reconcile script (PR3b) calls `resolve_disposition` per flight while
building the planner inputs, passing the per-flight state, whether the
flight is still in the active-flights index, and `now`.

Disposition precedence (first match wins):

  1. cancelled    — byAir `computed_status == "cancelled"`. Status wins
                    over membership and time: a cancelled leg is torn down
                    even if it is still in active-flights.
  2. diverted     — byAir `computed_status == "diverted"`.
  3. completed    — the flight is done: `computed_status == "landed"`, OR
                    its effective arrival instant is at/​before `now`. Its
                    managed events are historical; the planner leaves them.
  4. switched_away— not in active-flights AND still in the future (the user
                    switched flights upstream; byAir dropped it from the
                    index while the per-flight wake loop can no longer see
                    it). Torn down off the retained ledger tombstone.
  5. active       — in active-flights and not yet arrived. Reconciled
                    normally (boarding block + adopted flight event).

"Effective arrival" is the actual `last_snapshot.arr_time` when byAir has
published one, else the top-level `scheduled_arr_time`. Comparing arrival
(not departure) as the completion boundary keeps an in-air flight `active`
until it has actually landed.

stdlib-only (`datetime`) per `coding-policy: dependency-management`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from calendar_plan import (
    DISPOSITION_ACTIVE,
    DISPOSITION_CANCELLED,
    DISPOSITION_COMPLETED,
    DISPOSITION_DIVERTED,
    DISPOSITION_SWITCHED_AWAY,
)

STATUS_CANCELLED = "cancelled"
STATUS_DIVERTED = "diverted"
STATUS_LANDED = "landed"


class DispositionError(ValueError):
    """Raised when the flight state lacks a field the resolver needs.

    A ValueError subclass: the caller's fix is "pass a well-formed state
    record", not "retry". `scheduled_arr_time` is a required per-flight
    field (see `state-schema.md`), so its absence signals a malformed
    record, not a transient condition.
    """


def _to_instant(value: str, *, field: str) -> datetime:
    """Parse an RFC 3339 string to a timezone-aware UTC datetime.

    Raises DispositionError on a naive or unparseable value — a missing
    offset would make the at/​before-now comparison ambiguous, so it is a
    hard error rather than a silent local-time assumption (matches
    `calendar_plan._to_instant`).
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise DispositionError(f"{field} is not an RFC 3339 datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        raise DispositionError(f"{field} is missing a UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def _effective_arrival(flight_state: dict) -> datetime:
    """Actual arrival when byAir has published one, else scheduled arrival."""
    snapshot = flight_state.get("last_snapshot") or {}
    actual_arr = snapshot.get("arr_time") if isinstance(snapshot, dict) else None
    if actual_arr:
        return _to_instant(actual_arr, field="last_snapshot.arr_time")
    scheduled = flight_state.get("scheduled_arr_time")
    if not scheduled:
        raise DispositionError(
            "flight state has neither last_snapshot.arr_time nor scheduled_arr_time — "
            "cannot determine whether the flight has completed"
        )
    return _to_instant(scheduled, field="scheduled_arr_time")


def resolve_disposition(flight_state: dict, *, in_active_flights: bool, now: datetime) -> str:
    """Resolve one flight's disposition. See module docstring for precedence.

    Args:
        flight_state: the per-flight `flight-<id>.json` record.
        in_active_flights: whether the flight_id is still in
            `active-flights.json` (the per-flight wake loop only visits
            flights that are).
        now: the current instant; must be timezone-aware.

    Returns one of the `calendar_plan.DISPOSITION_*` constants.

    Raises:
        DispositionError: `now` is naive, or the state record lacks the
            arrival fields needed to decide completion.
    """
    if now.tzinfo is None:
        raise DispositionError("now must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)

    snapshot = flight_state.get("last_snapshot") or {}
    status = snapshot.get("computed_status") if isinstance(snapshot, dict) else None

    if status == STATUS_CANCELLED:
        return DISPOSITION_CANCELLED
    if status == STATUS_DIVERTED:
        return DISPOSITION_DIVERTED
    if status == STATUS_LANDED:
        return DISPOSITION_COMPLETED

    if _effective_arrival(flight_state) <= now_utc:
        return DISPOSITION_COMPLETED

    if not in_active_flights:
        return DISPOSITION_SWITCHED_AWAY
    return DISPOSITION_ACTIVE
