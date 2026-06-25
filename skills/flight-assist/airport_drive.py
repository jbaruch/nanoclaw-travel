"""Pure planner for the airport drive blocks — create / shift / skip decisions.

Piece 4a of #90. Given a flight's already-computed drive inputs (the I/O —
byAir airport context, Maps routing, the resolved origin — happens upstream in
the precheck, mirroring how `calendar_plan.py` receives a resolved
`boarding_lead_minutes`), this module decides whether to create a new airport
drive block, shift an existing one, or leave it as-is, and emits the ops.

It is a PURE function: no network, no clock reads beyond the injected `now`,
no I/O. The caller executes the returned ops (create / update via the
`airport_block` CREATE/PATCH contract) and writes the event IDs back into the
flight's `calendar_events` ledger.

Why not `calendar_plan.py`: that reconcile planner emits `{summary, start,
end, private_props}` bodies encoded via `calendar_tags` for byAir-calendar
events. The airport drive blocks use the self-contained `airport_block` codec
(full `build_block_args`, `<!--fadrive:-->` state) and live on the PRIMARY
calendar, create-first and re-anchored — the drive-planner model, not the
reconcile model. So the op body here IS the `build_block_args` dict.

Two block kinds, one per direction:
  - `airport_drive_dep` — drive TO the departure airport (to_airport).
  - `airport_drive_arr` — drive home from the arrival airport (from_airport).

stdlib-only (`datetime`) per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from airport_block import build_block_args  # noqa: E402

KIND_AIRPORT_DRIVE_DEP = "airport_drive_dep"
KIND_AIRPORT_DRIVE_ARR = "airport_drive_arr"

_DIRECTION_KIND = {
    "to_airport": KIND_AIRPORT_DRIVE_DEP,
    "from_airport": KIND_AIRPORT_DRIVE_ARR,
}


class AirportDrivePlanError(ValueError):
    """Raised when a desired-block input is missing a field the planner needs.

    A ValueError subclass: the caller's recovery is "pass a well-formed input",
    not "retry". The precheck resolves the routing/airport inputs before
    calling the planner, so this signals a caller bug, not bad calendar data.
    """


@dataclass(frozen=True)
class DesiredDriveBlock:
    """The block the caller wants on the calendar, with inputs already resolved.

    The precheck computes these from the flight snapshot + byAir airport context
    + Maps routing + the resolved origin (see module docstring). The planner
    turns this into create/shift/skip ops without any I/O.

    Fields:
        direction: "to_airport" or "from_airport".
        summary: human title (e.g. "Drive: → BNA (DL123)").
        leg_start: block start. to_airport: leave-by (`anchor − drive`);
            from_airport: the anchor itself.
        anchor: the deadline (to_airport: `dep − clearance`) or the earliest
            drive-home start (from_airport: `actual_arr + post_arrival_delay`).
        baseline_seconds: routed drive seconds.
        origin / destination: routed leg endpoints.
        leg_end: block end; defaults to `anchor` (to_airport). from_airport
            passes `anchor + drive`.
        timezone: the airport's IANA tz (for the CREATE), or None.
    """

    direction: str
    summary: str
    leg_start: datetime
    anchor: datetime
    baseline_seconds: int
    origin: str
    destination: str
    leg_end: datetime | None = None
    timezone: str | None = None

    @property
    def kind(self) -> str:
        kind = _DIRECTION_KIND.get(self.direction)
        if kind is None:
            raise AirportDrivePlanError(
                f"DesiredDriveBlock: unknown direction {self.direction!r} "
                f"(want one of {tuple(_DIRECTION_KIND)})"
            )
        return kind

    @property
    def _end(self) -> datetime:
        return self.leg_end if self.leg_end is not None else self.anchor

    def signature(self) -> str:
        """The `<start>/<end>` window pair the planner compares to decide no-op."""
        return f"{self.leg_start.isoformat()}/{self._end.isoformat()}"


def _make_op(
    *, op, kind, flight_id, calendar_id, reason, event_id=None, create_args=None, signature=None
):
    return {
        "op": op,
        "kind": kind,
        "flight_id": flight_id,
        "calendar_id": calendar_id,
        "event_id": event_id,
        "create_args": create_args,
        "signature": signature,
        "reason": reason,
    }


def plan_drive_block(
    *,
    flight_id,
    flight_code: str,
    desired: DesiredDriveBlock,
    ledger: dict,
    events_by_id: dict,
    calendar_id: str,
) -> list[dict]:
    """Reconcile one airport drive block against the ledger. Returns 0–1 ops.

    `ledger` is the flight's `calendar_events` map; `events_by_id` maps live
    event ids to their normalized fetched events (carrying a `signature` of the
    live `<start>/<end>`). The op `create_args` is the `airport_block`
    `build_block_args` dict; the executor passes it straight to CREATE (or, for
    an update, PATCH).

    - No ledger entry → create.
    - Tracked but missing from the calendar (deleted out of band) → recreate.
    - Live window + last-written signature both match desired → no-op.
    - Otherwise → update (re-anchor / re-route shifted the block).
    """
    kind = desired.kind
    create_args = build_block_args(
        calendar_id=calendar_id,
        flight_id=str(flight_id),
        direction=desired.direction,
        summary=desired.summary,
        leg_start=desired.leg_start,
        anchor=desired.anchor,
        baseline_seconds=desired.baseline_seconds,
        origin=desired.origin,
        destination=desired.destination,
        leg_end=desired.leg_end,
        timezone=desired.timezone,
    )
    desired_sig = desired.signature()
    start_iso = desired.leg_start.isoformat()
    entry = ledger.get(kind)

    if not entry:
        return [
            _make_op(
                op="create",
                kind=kind,
                flight_id=flight_id,
                calendar_id=calendar_id,
                create_args=create_args,
                signature=desired_sig,
                reason=f"no {kind} block tracked for {flight_code}; create at {start_iso}",
            )
        ]

    live = events_by_id.get(entry["event_id"])
    if live is None:
        return [
            _make_op(
                op="create",
                kind=kind,
                flight_id=flight_id,
                calendar_id=calendar_id,
                create_args=create_args,
                signature=desired_sig,
                reason=f"tracked {kind} block {entry['event_id']} missing; recreate",
            )
        ]

    if live.get("signature") == desired_sig and entry.get("synced_signature") == desired_sig:
        return []  # window already matches; nothing to write

    return [
        _make_op(
            op="update",
            kind=kind,
            flight_id=flight_id,
            calendar_id=entry["calendar_id"],
            event_id=entry["event_id"],
            create_args=create_args,
            signature=desired_sig,
            reason=f"shift {kind} block for {flight_code} to {start_iso}",
        )
    ]
