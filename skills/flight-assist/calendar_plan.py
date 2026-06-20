"""Pure reconciliation planner for flight-assist calendar events.

Given the current per-flight state (the `calendar_events` ledger + flight
facts) and a normalized snapshot of what is currently on the relevant
Google Calendars, compute the set of create / update / delete / adopt
operations that converge the calendar to the desired state. The planner
is a pure function: no network, no clock reads, no I/O. The caller
(`reconcile` script, PR3) fetches the events via Composio, runs this
planner, executes the returned ops, and writes the resulting event IDs
back into the ledger.

Everything deterministic lives here so it is unit-testable in CI without
any external service, per `coding-policy: script-delegation`.

byAir is the tile's single flight-data upstream and also the app that
writes the flight events onto the user's writable calendar (per
`rules/flight-data-locality.md`). TripIt's iCal feed is read-only and
never touched. Reclaim layers travel-time blocks on top.

Three classes of managed event, distinguished by which calendar they
live on (classification is by calendar ID, never summary regex):

  - boarding — flight-assist CREATES this (boarding-start -> departure)
    on the byAir calendar. `managed == "created"`; full lifecycle.
  - flight   — byAir CREATES this; flight-assist ADOPTS it by tagging,
    then shifts it delta-only and deletes it on a true cancel/switch.
    `managed == "adopted"`.
  - reclaim_travel — Reclaim CREATES this; flight-assist DELETES the
    bogus ones in a same-airport layover gap (positional rule). Gated on
    BOTH the Reclaim calendar ID and an `is_reclaim_travel` flag, since
    Reclaim's calendar also holds habit / focus / task blocks. Never
    tracked in the ledger (we delete, we do not own).

Ledger entry shape (see `state-schema.md`): per kind, a dict with
`event_id`, `calendar_id`, `managed`, `synced_signature`. The signature
is the `<start>/<end>` instant pair (normalized UTC) flight-assist last
wrote, so a reconcile is a no-op when the live event already matches.

stdlib-only (`datetime`) per `coding-policy: dependency-management`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Event-kind tags stamped into `extendedProperties.private`.
TAG_FLIGHT_ID = "faFlightId"
TAG_KIND = "faKind"
TAG_MANAGED = "faManaged"

KIND_BOARDING = "boarding"
KIND_FLIGHT = "flight"
KIND_RECLAIM_TRAVEL = "reclaim_travel"

MANAGED_CREATED = "created"
MANAGED_ADOPTED = "adopted"

# A flight is "active" and reconciled normally; any other disposition
# tears its managed events down (or leaves them, for a completed flight).
# The caller classifies disposition — it needs wall-clock + active-flights
# membership, which stay out of this pure planner.
DISPOSITION_ACTIVE = "active"
DISPOSITION_CANCELLED = "cancelled"
DISPOSITION_DIVERTED = "diverted"
DISPOSITION_SWITCHED_AWAY = "switched_away"
DISPOSITION_COMPLETED = "completed"

_TEARDOWN_DISPOSITIONS = frozenset(
    {DISPOSITION_CANCELLED, DISPOSITION_DIVERTED, DISPOSITION_SWITCHED_AWAY}
)

# How far apart a byAir-authored flight event's start may be from the
# flight's departure and still be considered the same leg when adopting.
# Wide enough to absorb a delay byAir already baked into the event before
# flight-assist first saw it; narrow enough not to collide with the next
# day's same-numbered flight. Same-code candidates are further tie-broken
# by closest start, so this is an outer bound, not the match precision.
ADOPT_MATCH_TOLERANCE_MINUTES = 360


class PlanError(ValueError):
    """Raised when a flight or event input is missing a field the planner needs.

    A ValueError subclass: the caller's recovery is "pass a well-formed
    input", not "retry". The reconcile script validates Composio output
    into the normalized shape before calling the planner, so a PlanError
    signals a caller bug, not bad calendar data.
    """


def _to_instant(value: str, *, field: str) -> datetime:
    """Parse an RFC 3339 string to a timezone-aware UTC datetime.

    Accepts a trailing `Z` or an explicit offset. Raises PlanError on a
    naive or unparseable value — every time the planner compares must be
    an unambiguous instant, so a missing offset is a hard error rather
    than a silent local-time assumption.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise PlanError(f"{field} is not an RFC 3339 datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        raise PlanError(f"{field} is missing a UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def _signature(start: str, end: str) -> str:
    """Canonical `<start>/<end>` signature in normalized UTC seconds.

    Offset-agnostic: the same instant written with `+00:00` or a local
    offset produces the same signature, so a re-serialization with a
    different-but-equal offset never reads as a change.
    """
    start_utc = _to_instant(start, field="start").strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = _to_instant(end, field="end").strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{start_utc}/{end_utc}"


def _require(mapping: dict, key: str, *, where: str):
    if key not in mapping:
        raise PlanError(f"{where} is missing required field {key!r}")
    return mapping[key]


def _event_signature(event: dict) -> str:
    return _signature(
        _require(event, "start", where=f"event {event.get('event_id')}"),
        _require(event, "end", where=f"event {event.get('event_id')}"),
    )


def _boarding_window(flight: dict) -> tuple[str, str]:
    """Desired boarding block: [departure - lead, departure], in dep's offset.

    Keeps the departure time's original UTC offset so the event a human
    sees is phrased in the airport's local time, not normalized UTC.
    """
    dep_raw = _require(flight, "dep_time", where=f"flight {flight.get('flight_id')}")
    # Parse preserving the original offset (not normalized to UTC) so the
    # written event stays in local airport time.
    dep_local = datetime.fromisoformat(dep_raw.replace("Z", "+00:00"))
    if dep_local.tzinfo is None:
        raise PlanError(f"flight {flight.get('flight_id')} dep_time missing offset: {dep_raw!r}")
    lead = _require(flight, "boarding_lead_minutes", where=f"flight {flight.get('flight_id')}")
    start_local = dep_local - timedelta(minutes=int(lead))
    return start_local.isoformat(), dep_local.isoformat()


def _make_op(*, op, kind, flight_id, calendar_id, reason, event_id=None, body=None, signature=None):
    return {
        "op": op,
        "kind": kind,
        "flight_id": flight_id,
        "calendar_id": calendar_id,
        "event_id": event_id,
        "body": body,
        "signature": signature,
        "reason": reason,
    }


def _boarding_body(flight: dict, start: str, end: str) -> dict:
    return {
        "summary": f"Boarding {flight['code']}",
        "start": start,
        "end": end,
        "private_props": {
            TAG_FLIGHT_ID: str(flight["flight_id"]),
            TAG_KIND: KIND_BOARDING,
            TAG_MANAGED: MANAGED_CREATED,
        },
    }


def _plan_boarding(flight: dict, events_by_id: dict, config: dict) -> list[dict]:
    """Reconcile the flight-assist-created boarding block for one active flight."""
    flight_id = flight["flight_id"]
    cal = _require(config, "boarding_calendar_id", where="config")
    start, end = _boarding_window(flight)
    desired_sig = _signature(start, end)
    body = _boarding_body(flight, start, end)
    ledger = flight.get("calendar_events", {}) or {}
    entry = ledger.get(KIND_BOARDING)

    if not entry:
        return [
            _make_op(
                op="create",
                kind=KIND_BOARDING,
                flight_id=flight_id,
                calendar_id=cal,
                body=body,
                signature=desired_sig,
                reason=f"no boarding block tracked for {flight['code']}; create at {start}",
            )
        ]

    live = events_by_id.get(entry["event_id"])
    if live is None:
        # Tracked but gone from the calendar (deleted out of band) — recreate.
        return [
            _make_op(
                op="create",
                kind=KIND_BOARDING,
                flight_id=flight_id,
                calendar_id=cal,
                body=body,
                signature=desired_sig,
                reason=f"tracked boarding block {entry['event_id']} missing; recreate",
            )
        ]

    if _event_signature(live) == desired_sig and entry.get("synced_signature") == desired_sig:
        return []  # delta-only: live event already matches desired window

    return [
        _make_op(
            op="update",
            kind=KIND_BOARDING,
            flight_id=flight_id,
            calendar_id=entry["calendar_id"],
            event_id=entry["event_id"],
            body=body,
            signature=desired_sig,
            reason=f"shift boarding block for {flight['code']} to {start}",
        )
    ]


def _match_byair_event(flight: dict, events: list[dict], config: dict) -> dict | None:
    """Find the byAir-authored flight event for this leg, to adopt it.

    Candidates: events on the byAir calendar whose summary contains the
    flight code and whose start is within ADOPT_MATCH_TOLERANCE_MINUTES of
    the flight's departure. The closest by start wins, so two legs sharing
    a code on different days do not cross-match. Already-tagged events are
    skipped — adoption keys off the ledger after the first match, never by
    re-matching a moved event.
    """
    byair_cal = _require(config, "byair_calendar_id", where="config")
    dep = _to_instant(
        _require(flight, "dep_time", where=f"flight {flight['flight_id']}"), field="dep_time"
    )
    code = flight["code"]
    best = None
    best_delta = None
    for event in events:
        if event.get("calendar_id") != byair_cal:
            continue
        if code not in (event.get("summary") or ""):
            continue
        if (event.get("private_props") or {}).get(TAG_FLIGHT_ID):
            continue  # already tagged/owned — not an adoption candidate
        start = _to_instant(
            _require(event, "start", where=f"event {event.get('event_id')}"), field="start"
        )
        delta = abs((start - dep).total_seconds()) / 60.0
        if delta > ADOPT_MATCH_TOLERANCE_MINUTES:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = event, delta
    return best


def _plan_flight_event(
    flight: dict, events: list[dict], events_by_id: dict, config: dict
) -> list[dict]:
    """Adopt and delta-shift the byAir-authored flight event for one active flight."""
    flight_id = flight["flight_id"]
    dep = _require(flight, "dep_time", where=f"flight {flight_id}")
    arr = _require(flight, "arr_time", where=f"flight {flight_id}")
    desired_sig = _signature(dep, arr)
    ledger = flight.get("calendar_events", {}) or {}
    entry = ledger.get(KIND_FLIGHT)

    if not entry:
        match = _match_byair_event(flight, events, config)
        if match is None:
            return []  # byAir has not written the event yet, or it is elsewhere
        return [
            _make_op(
                op="adopt",
                kind=KIND_FLIGHT,
                flight_id=flight_id,
                calendar_id=match["calendar_id"],
                event_id=match["event_id"],
                body={
                    "private_props": {
                        TAG_FLIGHT_ID: str(flight_id),
                        TAG_KIND: KIND_FLIGHT,
                        TAG_MANAGED: MANAGED_ADOPTED,
                    }
                },
                signature=_event_signature(match),
                reason=f"adopt byAir flight event {match['event_id']} for {flight['code']}",
            )
        ]

    live = events_by_id.get(entry["event_id"])
    if live is None:
        # Adopted event vanished from the calendar — byAir owns its
        # existence, so drop our tombstone rather than recreating it.
        return [
            _make_op(
                op="forget",
                kind=KIND_FLIGHT,
                flight_id=flight_id,
                calendar_id=entry["calendar_id"],
                event_id=entry["event_id"],
                reason=f"adopted flight event {entry['event_id']} gone; stop tracking",
            )
        ]

    if _event_signature(live) == desired_sig:
        return []  # byAir already shifted it (or we did) — no-op

    return [
        _make_op(
            op="update",
            kind=KIND_FLIGHT,
            flight_id=flight_id,
            calendar_id=entry["calendar_id"],
            event_id=entry["event_id"],
            body={"start": dep, "end": arr},
            signature=desired_sig,
            reason=f"byAir left {flight['code']} event stale; shift to {dep}",
        )
    ]


def _plan_reclaim_deletions(
    trip_flights: list[dict], events: list[dict], config: dict
) -> list[dict]:
    """Delete bogus Reclaim travel blocks in same-airport layover gaps.

    Positional rule: between leg-N and leg-(N+1) of one trip, a same
    departure/arrival airport means no ground transfer, so a Reclaim
    travel block sitting in that gap is bogus. Different airports mean a
    real inter-airport transfer — keep it. Travel before the first leg
    and after the last leg is never in a gap, so it is never deleted.

    Two guards bound every delete: the event must be on the Reclaim
    calendar AND carry `is_reclaim_travel`. Reclaim's calendar also holds
    habit / focus / task blocks, so calendar membership alone is not a
    safe delete signal — `is_reclaim_travel` is set upstream by the
    reconcile script's Reclaim classifier, and the planner refuses to
    delete an unflagged event. User events on other calendars are never
    candidates at all.
    """
    reclaim_cal = _require(config, "reclaim_calendar_id", where="config")
    legs = sorted(
        trip_flights,
        key=lambda f: _to_instant(
            _require(f, "dep_time", where=f"flight {f.get('flight_id')}"), field="dep_time"
        ),
    )
    ops: list[dict] = []
    for leg_n, leg_next in zip(legs, legs[1:]):
        where_n = f"flight {leg_n.get('flight_id')}"
        where_next = f"flight {leg_next.get('flight_id')}"
        if _require(leg_n, "arr_airport_id", where=where_n) != _require(
            leg_next, "dep_airport_id", where=where_next
        ):
            continue  # different airports -> legitimate transfer, keep
        gap_start = _to_instant(_require(leg_n, "arr_time", where=where_n), field="arr_time")
        gap_end = _to_instant(_require(leg_next, "dep_time", where=where_next), field="dep_time")
        for event in events:
            if event.get("calendar_id") != reclaim_cal:
                continue
            if not event.get("is_reclaim_travel"):
                continue  # Reclaim calendar also holds habit/focus/task blocks — never delete those
            ev_start = _to_instant(
                _require(event, "start", where=f"event {event.get('event_id')}"), field="start"
            )
            ev_end = _to_instant(
                _require(event, "end", where=f"event {event.get('event_id')}"), field="end"
            )
            if ev_start >= gap_start and ev_end <= gap_end:
                ops.append(
                    _make_op(
                        op="delete",
                        kind=KIND_RECLAIM_TRAVEL,
                        flight_id=_require(leg_next, "flight_id", where=where_next),
                        calendar_id=reclaim_cal,
                        event_id=event["event_id"],
                        reason=(
                            f"Reclaim travel block in same-airport layover "
                            f"({_require(leg_n, 'code', where=where_n)} -> "
                            f"{_require(leg_next, 'code', where=where_next)}); delete"
                        ),
                    )
                )
    return ops


def _plan_teardown(flight: dict) -> list[dict]:
    """Delete flight-assist-managed events for a cancelled / switched flight."""
    flight_id = flight["flight_id"]
    ledger = flight.get("calendar_events", {}) or {}
    ops: list[dict] = []

    boarding = ledger.get(KIND_BOARDING)
    if boarding:
        ops.append(
            _make_op(
                op="delete",
                kind=KIND_BOARDING,
                flight_id=flight_id,
                calendar_id=boarding["calendar_id"],
                event_id=boarding["event_id"],
                reason=f"{flight['code']} {flight['disposition']}; delete boarding block",
            )
        )

    adopted = ledger.get(KIND_FLIGHT)
    if adopted:
        # byAir leaves the stale event behind on a switch/cancel; ayeaye
        # takes over and removes it. Only acts on the event it adopted.
        ops.append(
            _make_op(
                op="delete",
                kind=KIND_FLIGHT,
                flight_id=flight_id,
                calendar_id=adopted["calendar_id"],
                event_id=adopted["event_id"],
                reason=f"{flight['code']} {flight['disposition']}; remove stale byAir flight event",
            )
        )
    return ops


def plan_reconciliation(flights: list[dict], events: list[dict], config: dict) -> list[dict]:
    """Compute the calendar reconciliation plan for the given flights.

    Pure function. `flights` are caller-derived per-flight inputs (flight
    facts + the `calendar_events` ledger + a `disposition` + a resolved
    `boarding_lead_minutes`); `events` is the normalized list of current
    calendar events across the byAir and Reclaim calendars; `config`
    carries the calendar IDs only. The boarding-lead policy (aircraft size
    + TATL/TPAC) is resolved upstream by `boarding_lead.py` and arrives
    here as a per-flight integer, so the volatile policy stays out of the
    planner. Returns an ordered op list — see module docstring for op
    shapes. The caller executes them and writes event IDs back to the
    ledger.

    Order: teardown deletes first, then per-active-flight boarding and
    flight-event ops, then per-trip Reclaim deletions. Deterministic given
    deterministic inputs (flights are processed in input order; trips in
    first-seen order).
    """
    events_by_id = {e["event_id"]: e for e in events if e.get("event_id")}
    ops: list[dict] = []

    active: list[dict] = []
    for flight in flights:
        disposition = _require(flight, "disposition", where=f"flight {flight.get('flight_id')}")
        if disposition == DISPOSITION_ACTIVE:
            active.append(flight)
        elif disposition in _TEARDOWN_DISPOSITIONS:
            ops.extend(_plan_teardown(flight))
        # DISPOSITION_COMPLETED: leave managed events as a record; no ops.

    for flight in active:
        ops.extend(_plan_boarding(flight, events_by_id, config))
        ops.extend(_plan_flight_event(flight, events, events_by_id, config))

    # Reclaim deletions are per-trip over active legs (a cancelled leg no
    # longer shapes the layover). Group by trip_id, first-seen order.
    trips: dict[int, list[dict]] = {}
    for flight in active:
        trips.setdefault(
            _require(flight, "trip_id", where=f"flight {flight['flight_id']}"), []
        ).append(flight)
    for trip_flights in trips.values():
        if len(trip_flights) >= 2:
            ops.extend(_plan_reclaim_deletions(trip_flights, events, config))

    return ops
