"""Reconcile diff — desired legs vs current calendar blocks — pure, no I/O.

Computes the create / update / delete / convert actions that make the calendar
match the engine's desired legs. This is the heart of the unified reconcile and of
shadow mode (#156 R4): the I/O layer applies the returned plan (or, in shadow mode,
just logs it) — the decision of WHAT to change lives here and is fully testable.

Three defect classes from the design collapse into this one diff:

- **Duplicate storm (G1).** Current blocks are indexed by leg identity, and ALL
  blocks sharing an identity are found — one is kept/updated, every extra is
  deleted. A single find-existing can't collapse an N-way storm; scanning all can.
- **Legacy convergence (R4 cutover).** A prior-gen block (fadrive / dp) that
  matches a desired leg is CONVERTED — the new block is created and every matching
  legacy event deleted, atomically, never double-stamped. Because a desired leg
  declares the legacy keys it supersedes (every byAir id of a codeshare, both
  directions), the dual-id storm maps cleanly onto one desired leg.
- **Orphan deletion (G7).** A current block — unified or legacy — that matches NO
  desired leg is deleted, not merely left alone. This is what clears a suppressed
  connection's stale drives (the 7× CPH storm) and pre-filter orphans, rather than
  only ceasing to create.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from block_codec import GEN_LEGACY_FADRIVE, GEN_UNIFIED, ParsedBlock

# Legacy directions ↔ unified leg kinds, for building a desired leg's legacy keys.
_KIND_TO_LEGACY_DIRECTION = {
    "airport_departure": "to_airport",
    "airport_arrival": "from_airport",
}

# The legacy block generations the engine currently OWNS and may converge or
# orphan-delete. Today the engine produces only airport legs, so it owns just the
# flight-assist airport blocks (fadrive). Drive-planner MEETING blocks (dp) are NOT
# owned yet — the engine has no meeting-leg source — so they are left completely
# untouched (never orphaned), never mistaken for airport-leg garbage. When the
# meeting source lands, add GEN_LEGACY_DP to the managed set.
_DEFAULT_MANAGED_LEGACY = frozenset({GEN_LEGACY_FADRIVE})


@dataclass(frozen=True)
class DesiredBlock:
    """One drive block the engine wants to exist. `legacy_keys` are the
    `(generation, legacy_id, direction)` triples of prior-gen blocks this leg
    supersedes — every byAir id of a codeshare, its direction — so cutover
    converts them onto this one leg."""

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
    legacy_keys: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)


def legacy_keys_for_airport_leg(
    kind: str, byair_flight_ids: frozenset[int]
) -> frozenset[tuple[str, str, str]]:
    """The fadrive legacy keys a unified airport leg supersedes — one per byAir id.

    A codeshare tracked under two byAir ids yields two keys, so both legacy blocks
    converge onto the single unified leg. Transfer (a new concept with no legacy
    equivalent) gets no keys — any nearby legacy block is an orphan.
    """
    direction = _KIND_TO_LEGACY_DIRECTION.get(kind)
    if direction is None:
        return frozenset()
    return frozenset((GEN_LEGACY_FADRIVE, str(fid), direction) for fid in byair_flight_ids)


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


@dataclass(frozen=True)
class Delete:
    event_id: str | None
    reason: str


@dataclass(frozen=True)
class Convert:
    """Adopt a prior-gen leg: create the new block and delete every matching legacy
    event, atomically (#156 R4 — never accumulate)."""

    desired: DesiredBlock
    legacy_event_ids: tuple[str | None, ...]


@dataclass(frozen=True)
class ReconcilePlan:
    creates: tuple[Create, ...] = ()
    updates: tuple[Update, ...] = ()
    deletes: tuple[Delete, ...] = ()
    converts: tuple[Convert, ...] = ()

    @property
    def is_noop(self) -> bool:
        return not (self.creates or self.updates or self.deletes or self.converts)


def _legacy_key(block: ParsedBlock) -> tuple[str, str, str] | None:
    if block.legacy_id is None or block.legacy_direction is None:
        return None
    return (block.generation, block.legacy_id, block.legacy_direction)


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


def material_update_delta(prior_seconds: int | None, new_seconds: int) -> tuple[int, str] | None:
    """Classify a drive-duration change for operator alerting.

    Returns `(minutes, direction)` when the change is material — at least
    `_MATERIAL_UPDATE_FRACTION` of the prior duration AND at least one whole
    minute — else None (routine jitter, alert suppressed). `direction` is
    `"sooner"` when the drive got LONGER (leave earlier) and `"later"` when it
    got shorter (leave later). A missing / non-positive prior duration can't be
    compared, so it is never material.
    """
    if prior_seconds is None or prior_seconds <= 0:
        return None
    diff = new_seconds - prior_seconds
    if abs(diff) / prior_seconds < _MATERIAL_UPDATE_FRACTION:
        return None
    # FLOOR, not round: "at least one whole minute" means a real >= 60s swing.
    # Rounding would report "1 min" for a 31-59s change and wake on it (a short
    # drive can clear the 10% bar with well under a minute of movement).
    minutes = abs(diff) // 60
    if minutes < 1:
        return None
    return minutes, ("sooner" if diff > 0 else "later")


def _needs_update(current: ParsedBlock, desired: DesiredBlock) -> bool:
    """Whether a matched unified block differs from the desired leg on the fields
    that drive a shift: anchor, endpoints, window, OR a MEANINGFUL change in the
    routed drive duration (`baseline_seconds`). A real route-duration change
    (traffic grew) moves the block's leave-by and must trigger an update; a
    sub-tolerance change is routing jitter, not signal, and is ignored so the
    live writer doesn't churn a recreate every sweep (#164)."""
    if (
        current.anchor != desired.anchor
        or current.origin != desired.origin
        or current.destination != desired.destination
        or current.window_end != desired.window_end
    ):
        return True
    # A block whose baseline didn't parse can't be compared — re-shift it to a
    # well-formed block. Otherwise update only on a meaningful (>= tolerance)
    # drive-duration change, ignoring routing jitter (#164).
    if current.baseline_seconds is None:
        return True
    drift = abs(current.baseline_seconds - desired.baseline_seconds)
    return drift >= _BASELINE_SHIFT_TOLERANCE_SECONDS


def plan_reconcile(
    desired: list[DesiredBlock],
    current: list[ParsedBlock],
    *,
    managed_legacy: frozenset[str] = _DEFAULT_MANAGED_LEGACY,
) -> ReconcilePlan:
    """Diff desired legs against current calendar blocks (#156 G1 / G7 / R4).

    `managed_legacy` is the set of legacy generations the engine owns and may
    converge or orphan-delete; blocks of any other legacy generation (e.g.
    drive-planner meeting blocks while the engine has no meeting-leg source) are
    left entirely untouched — never orphaned. Pure and deterministic: output
    action lists are ordered by the desired-leg order then the current-block
    order, with no clock or ambient state.
    """
    creates: list[Create] = []
    updates: list[Update] = []
    deletes: list[Delete] = []
    converts: list[Convert] = []

    # Index current blocks: unified by (identity, kind), legacy by their key.
    unified_by_key: dict[tuple[str | None, str | None], list[ParsedBlock]] = {}
    legacy_by_key: dict[tuple[str, str, str], list[ParsedBlock]] = {}
    for block in current:
        if block.generation == GEN_UNIFIED and block.identity is not None:
            unified_by_key.setdefault((block.identity, block.kind), []).append(block)
        elif block.generation in managed_legacy:
            key = _legacy_key(block)
            if key is not None:
                legacy_by_key.setdefault(key, []).append(block)

    desired_keys: set[tuple[str, str]] = set()
    consumed_legacy: set[int] = set()  # id() of legacy blocks turned into converts

    for d in desired:
        key = (d.identity, d.kind)
        desired_keys.add(key)
        matches = unified_by_key.get(key, [])
        if matches:
            # G1: keep the first, delete every extra sharing this identity.
            keep = matches[0]
            if _needs_update(keep, d):
                updates.append(Update(keep.event_id, d, keep.baseline_seconds))
            for extra in matches[1:]:
                deletes.append(Delete(extra.event_id, "duplicate identity"))
            continue

        # No unified block: converge any legacy blocks this leg supersedes.
        legacy_events: list[str | None] = []
        for lk in d.legacy_keys:
            for block in legacy_by_key.get(lk, []):
                if id(block) not in consumed_legacy:
                    consumed_legacy.add(id(block))
                    legacy_events.append(block.event_id)
        if legacy_events:
            converts.append(Convert(d, tuple(legacy_events)))
        else:
            creates.append(Create(d))

    # Unified orphans: current unified blocks with no desired leg.
    for (identity, kind), blocks in unified_by_key.items():
        if (identity, kind) not in desired_keys:
            for block in blocks:
                deletes.append(Delete(block.event_id, "orphan: no desired leg"))

    # Legacy orphans: any legacy block not consumed by a convert.
    for blocks in legacy_by_key.values():
        for block in blocks:
            if id(block) not in consumed_legacy:
                deletes.append(Delete(block.event_id, "legacy orphan: no desired leg"))

    return ReconcilePlan(
        creates=tuple(creates),
        updates=tuple(updates),
        deletes=tuple(deletes),
        converts=tuple(converts),
    )
