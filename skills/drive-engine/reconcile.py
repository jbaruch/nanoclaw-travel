"""Reconcile diff — desired legs vs current calendar blocks — pure, no I/O.

Computes the create / update / delete actions that make the calendar
match the engine's desired legs. This is the heart of the unified reconcile and of
shadow mode (#156 R4): the I/O layer applies the returned plan (or, in shadow mode,
just logs it) — the decision of WHAT to change lives here and is fully testable.

Three defect classes from the design collapse into this one diff:

- **Duplicate storm (G1).** Current blocks are indexed by leg identity, and ALL
  blocks sharing an identity are found — one is kept/updated, every extra is
  deleted. A single find-existing can't collapse an N-way storm; scanning all can.
- **Orphan deletion (G7).** A current block that matches NO
  desired leg is deleted, not merely left alone. This is what clears a suppressed
  connection's stale drives (the 7× CPH storm) and pre-filter orphans, rather than
  only ceasing to create.

A leg the engine could not compute this sweep — its route failed, its position
was unresolved, its drive came out implausible, or its drive window has passed —
is HELD, not gone (#334). Its existing blocks are left exactly as they are:
neither updated nor orphan-deleted. Only a leg that no longer exists (the flight
or meeting is gone, or a deliberate suppression such as a trivial drive) lets
its block be deleted. A provider blip used to read as "this drive no longer
exists" and deleted thirteen correct blocks in one sweep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from block_codec import (
    GEN_UNIFIED,
    ParsedBlock,
    human_description,
    parse_ev_note,
)

DEPART_AFTER_KINDS = frozenset(
    {"meeting_return", "airport_arrival", "lodging_return", "attendance_return"}
)


@dataclass(frozen=True)
class DesiredBlock:
    """One drive block the engine wants to exist."""

    identity: str
    kind: str
    summary: str
    start: datetime
    end: datetime
    origin: str
    destination: str
    baseline_seconds: int
    anchor: datetime
    window_end: datetime | None = None
    timezone: str | None = None  # IANA tz the block is created in (local display)
    # True when `timezone` is the operator's current zone from the core reader
    # (#309). Only then does a zone difference on an existing block call for a
    # patch: a fallback zone (the reader was unavailable) must not flap every
    # block back and forth whenever the reader flickers.
    zone_from_operator: bool = False
    charging_note: str | None = None
    earliest_start: datetime | None = None
    # A long meeting drive planned because TripIt has no flight or train (#377).
    road_trip: bool = False
    # The talk start when this drive arrives at the talk's talk-prep Prep hold
    # instead (#381): `anchor` is then the Prep start. Notice-only, never
    # persisted, so the block itself does not change shape.
    prep_for: datetime | None = None


@dataclass(frozen=True)
class HeldLeg:
    """A leg that exists but could not be computed this sweep (#334).

    `identity` and `kind` key it like a `DesiredBlock`, so its current blocks are
    found and left untouched. `summary`, `when` (the leg's anchor) and `reason`
    describe it for the operator when no block exists to hold. `announce` is
    False for a hold that only keeps blocks in place (a meeting under way, a
    local drive an outer leg may have absorbed, a flight byAir sent unreadable):
    with no block it is not a drive the engine failed to plan, so it is never
    reported missing.
    """

    identity: str
    kind: str
    summary: str
    when: datetime | None
    reason: str
    announce: bool = True
    # Source-proved earliest departure; None never invents a route duration.
    not_before: datetime | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.identity, self.kind)


@dataclass(frozen=True)
class Create:
    desired: DesiredBlock


@dataclass(frozen=True)
class Update:
    event_id: str | None
    desired: DesiredBlock
    # The matched block's prior routed drive duration, so the apply layer can tell
    # a material traffic change (worth alerting the operator) from routine jitter.
    prior_baseline_seconds: int | None = None
    # Native Calendar span for EV notices, separate from persisted route seconds.
    prior_effective_seconds: int | None = None


@dataclass(frozen=True)
class Delete:
    event_id: str | None
    reason: str


@dataclass(frozen=True)
class ReconcilePlan:
    creates: tuple[Create, ...] = ()
    updates: tuple[Update, ...] = ()
    deletes: tuple[Delete, ...] = ()

    @property
    def is_noop(self) -> bool:
        return not (self.creates or self.updates or self.deletes)


# A matched block is only shifted when its routed drive duration changed by at
# least this much. The maps route returns a slightly different `baseline_seconds`
# on every sweep (traffic recomputation jitter — observed 1–46s swings for an
# unchanged leg); comparing exactly made every sweep re-shift all ~15 legs. That
# churn drove the #164 duplicate storm back when a shift was a recreate-then-
# delete (a sweep killed between the create and the delete duplicated the leg).
# Shifts are now an in-place PATCH (calendar_apply) so they can no longer
# duplicate, but re-patching every leg every sweep for sub-minute noise is still
# pointless churn — so ignore it. 2 min is well above the jitter yet still
# catches a real traffic change (which moves the leave-by enough to matter).
_BASELINE_SHIFT_TOLERANCE_SECONDS = 120

# A shifted block only ALERTS the operator when the routed drive duration changed
# by at least this fraction of its prior value — a material traffic swing worth a
# "leave earlier/later" heads-up, versus the routine sub-tolerance re-times that
# happen every sweep and are pure noise. Below this, the block is still updated
# silently so the calendar stays accurate; only the notification is suppressed.
_MATERIAL_UPDATE_FRACTION = 0.10

# ...and by at least this many seconds in absolute terms. A percentage alone
# fires on a short drive for a swing too small to act on: a 20-minute commute
# drifting 2 minutes is 10% and cleared the old gate, which is how "leave 4
# minutes earlier" reached the operator. Ten minutes is the point where the
# advice changes what someone actually does.
#
# MUST stay >= `_BASELINE_SHIFT_TOLERANCE_SECONDS`: below that the reconcile
# schedules no Update at all, so the change never reaches `apply_plan` to be
# notified and the alert would promise a heads-up the sweep cannot deliver.
_MATERIAL_UPDATE_FLOOR_SECONDS = 600

# ...and only once the drive is near enough to act on. A traffic re-route two
# months out is not information — the routed duration will be recomputed dozens
# of times before the operator leaves, and every intermediate swing was being
# announced. Inside two hours the number is close to final and there is still
# room to leave earlier.
_MATERIAL_UPDATE_HORIZON = timedelta(hours=2)


def material_update_delta(
    prior_seconds: int | None,
    new_seconds: int,
    *,
    starts_at: datetime,
    now: datetime,
) -> tuple[int, str] | None:
    """Classify a drive-duration change for operator alerting.

    Returns `(minutes, direction)` when the change is material, else None.
    `direction` is `"sooner"` when the drive got LONGER (leave earlier) and
    `"later"` when it got shorter (leave later). A missing / non-positive prior
    duration can't be compared, so it is never material.

    `starts_at` is when the block begins — the moment the operator acts on it —
    and is compared against `now` for the horizon gate. Both are required: a
    duration change carries no meaning without knowing when the drive is, and
    defaulting either would silently restore the far-future noise.

    Material requires ALL of:
      - the drive begins within `_MATERIAL_UPDATE_HORIZON`. Further out the
        number is provisional and will move again before it matters.
      - a swing of at least `_MATERIAL_UPDATE_FLOOR_SECONDS` — big enough to
        change what the operator does, and (being >= the reconcile's patch
        tolerance) big enough that an Update was actually scheduled.
      - a swing of at least `_MATERIAL_UPDATE_FRACTION` of the prior duration —
        a proportionally real traffic change, not a large absolute move on an
        already-long drive.

    A block already under way (`starts_at` at or before `now`) passes the
    horizon: the drive is as imminent as it gets.
    """
    if prior_seconds is None or prior_seconds <= 0:
        return None
    if starts_at - now > _MATERIAL_UPDATE_HORIZON:
        return None
    diff = new_seconds - prior_seconds
    if abs(diff) < _MATERIAL_UPDATE_FLOOR_SECONDS:
        return None
    if abs(diff) / prior_seconds < _MATERIAL_UPDATE_FRACTION:
        return None
    # FLOOR, not round: a reported minute is a real >= 60s of that minute.
    return abs(diff) // 60, ("sooner" if diff > 0 else "later")


def _needs_update(current: ParsedBlock, desired: DesiredBlock) -> bool:
    """Whether a matched unified block differs from the desired leg on the fields
    that drive a shift: anchor, endpoints, window, the operator display zone
    (#309), OR a MEANINGFUL change in the routed drive duration
    (`baseline_seconds`). A real route-duration change
    (traffic grew) moves the block's leave-by and must trigger an update; a
    sub-tolerance change is routing jitter, not signal, and is ignored so the
    live writer doesn't churn a recreate every sweep (#164)."""
    had_ev_note = (
        parse_ev_note(current.description, current.origin, current.destination) is not None
    )
    if desired.charging_note is not None or had_ev_note:
        # Add/change/remove a known EV allowance even when its minute-sized
        # duration is below the ordinary traffic jitter threshold. Repeated
        # identical renders remain a no-op; schema-1 private state is unchanged.
        if current.description != human_description(
            desired.origin, desired.destination, desired.charging_note
        ):
            return True
        if (
            current.duration_seconds is None
            or abs(current.duration_seconds - int((desired.end - desired.start).total_seconds()))
            >= _BASELINE_SHIFT_TOLERANCE_SECONDS
        ):
            return True
    if (
        current.anchor != desired.anchor
        or current.origin != desired.origin
        or current.destination != desired.destination
        or current.window_end != desired.window_end
    ):
        return True
    # A block displayed in a zone other than the operator's current one
    # converges to it (#309). Only a zone the operator reader supplied counts,
    # and only against a block whose own zone was read back.
    if (
        desired.zone_from_operator
        and current.timezone is not None
        and current.timezone != desired.timezone
    ):
        return True
    # A block whose baseline didn't parse can't be compared — re-shift it to a
    # well-formed block. Otherwise update only on a meaningful (>= tolerance)
    # drive-duration change, ignoring routing jitter (#164).
    if current.baseline_seconds is None:
        return True
    drift = abs(current.baseline_seconds - desired.baseline_seconds)
    return drift >= _BASELINE_SHIFT_TOLERANCE_SECONDS


def _done_by(block: ParsedBlock) -> datetime | None:
    """When a block's drive is behind the operator: a transfer's at the end of
    its window (its anchor is the landing that opens it), any other's at its
    anchor."""
    if block.kind == "airport_transfer" and block.window_end is not None:
        return block.window_end
    return block.anchor


def plan_reconcile(
    desired: list[DesiredBlock],
    current: list[ParsedBlock],
    *,
    held: frozenset[tuple[str, str]] = frozenset(),
    now: datetime | None = None,
) -> ReconcilePlan:
    """Diff desired legs against current calendar blocks (#156 G1 / G7 / R4).

    `held` are the `(identity, kind)` keys
    of legs that exist but could not be computed this sweep (`HeldLeg`): their
    current unified blocks are neither updated nor deleted. With `now`, a
    unified block whose drive is behind the operator (`_done_by`) is history
    and is never orphan-deleted: a meeting that ended drops out of the calendar fetch, and
    its drive stays where it happened. Pure and deterministic: output action
    lists are ordered by the desired-leg order then the current-block order, and
    the only clock is the `now` passed in.
    """
    creates: list[Create] = []
    updates: list[Update] = []
    deletes: list[Delete] = []

    # Index current blocks by (identity, kind).
    unified_by_key: dict[tuple[str | None, str | None], list[ParsedBlock]] = {}
    for block in current:
        if block.generation == GEN_UNIFIED and block.identity is not None:
            unified_by_key.setdefault((block.identity, block.kind), []).append(block)

    desired_keys: set[tuple[str, str]] = set()

    for d in desired:
        key = (d.identity, d.kind)
        desired_keys.add(key)
        matches = unified_by_key.get(key, [])
        if matches:
            # G1: keep the first, delete every extra sharing this identity.
            keep = matches[0]
            if _needs_update(keep, d):
                has_ev = (
                    d.charging_note is not None
                    or parse_ev_note(keep.description, keep.origin, keep.destination) is not None
                )
                updates.append(
                    Update(
                        keep.event_id,
                        d,
                        keep.baseline_seconds,
                        keep.duration_seconds if has_ev else None,
                    )
                )
            for extra in matches[1:]:
                deletes.append(Delete(extra.event_id, "duplicate identity"))
            continue

        creates.append(Create(d))

    # Unified orphans: current unified blocks with no desired leg. A held leg
    # still exists, so its blocks are not orphans (#334).
    for (identity, kind), blocks in unified_by_key.items():
        if (identity, kind) not in desired_keys and (identity, kind) not in held:
            for block in blocks:
                done_by = _done_by(block)
                if now is not None and done_by is not None and done_by < now:
                    continue  # history, not an orphan
                deletes.append(Delete(block.event_id, "orphan: no desired leg"))

    return ReconcilePlan(
        creates=tuple(creates),
        updates=tuple(updates),
        deletes=tuple(deletes),
    )
