"""Apply a reconcile plan to the calendar — the engine's WRITE path.

Executes a `ReconcilePlan` against a Composio calendar client: deletes orphans,
creates new drive blocks, converts prior-gen blocks (create new + delete legacy),
and shifts changed blocks with an in-place PATCH. This is what makes the engine
authoritative instead of merely observant.

A shift is a single atomic PATCH of the same event — never a recreate-then-delete
— so a sweep killed mid-write can't leave the new block next to an undeleted old
one (the #164 duplicate storm). A convert still CREATES the unified replacement
before deleting the legacy event(s) (they are distinct events), rolling the
replacement back if a legacy delete fails; a delete that 404s (already gone) is an
idempotent success. The whole write phase is bounded by a wall-clock budget so it
returns a clean payload under the host precheck timeout, deferring the rest to the
next (idempotent) sweep instead of being killed mid-write (#164).

Deletes need only an event id, so the drive-planner cleanup runs through the delete
path with no create/timezone concerns. The create path builds the v3 flat
`start_datetime` + duration args and carries the unified codec description so the
block round-trips.
"""

from __future__ import annotations

import sys
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import build_description  # noqa: E402
from reconcile import DesiredBlock, ReconcilePlan, material_update_delta  # noqa: E402

# A drive block's human summary is "Drive: <meeting/leg>". Stripping the prefix
# recovers the name the operator recognizes for a notification.
_DRIVE_SUMMARY_PREFIX = "Drive: "

# flight-assist ships ComposioError; resolve it cross-bundle for the caught type.
_FA = Path("/home/node/.claude/skills/tessl__flight-assist")
if not _FA.is_dir():
    _FA = _BUNDLE_DIR.parent / "flight-assist"
if str(_FA) not in sys.path:
    sys.path.insert(0, str(_FA))

from calendar_reconcile import _created_event_id  # noqa: E402
from composio_client import ComposioError  # noqa: E402

_WRITE_ERRORS = (ComposioError, urllib.error.URLError)


@dataclass
class ApplyResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    converted: int = 0
    deferred: int = 0
    errors: list[str] = field(default_factory=list)
    # Operator-facing notification material, recorded ONLY for ops that actually
    # applied this sweep (never for deferred / failed ones). Per-leg here; the
    # sweep payload builder groups them per meeting. `added_meeting_legs` holds
    # only MEETING creates — airport creates are not notified (a flight drive is
    # not skippable, so there is nothing for the operator to act on).
    added_meeting_legs: list[dict] = field(default_factory=list)
    material_updates: list[dict] = field(default_factory=list)

    @property
    def total_writes(self) -> int:
        return self.created + self.updated + self.deleted + self.converted


def _meeting_name(summary: str) -> str:
    """Recover the operator-recognizable name from a "Drive: <name>" summary."""
    if summary.startswith(_DRIVE_SUMMARY_PREFIX):
        return summary[len(_DRIVE_SUMMARY_PREFIX) :]
    return summary


def _local_when(desired: DesiredBlock) -> str:
    """The block's meeting-relevant time (its anchor) as a human local string,
    e.g. "Sat Jul 18, 10:35" — what the operator sees for "... at 10:35"."""
    local_dt, _ = _start_in_local(desired.anchor, desired.timezone)
    return local_dt.strftime("%a %b %d, %H:%M")


def _start_in_local(start: datetime, tz_name: str | None) -> tuple[datetime, str]:
    """Express `start` (a UTC instant) as wall-clock in `tz_name`, or UTC.

    A meeting/airport block created in its local IANA tz shows the right local
    time on the calendar (an 08:45 Tennessee practice reads 08:45, not a foreign
    offset). An unknown / missing tz falls back to unambiguous UTC.
    """
    if tz_name:
        try:
            return start.astimezone(ZoneInfo(tz_name)), tz_name
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return start.astimezone(timezone.utc), "UTC"


def build_create_args(desired: DesiredBlock, *, calendar_id: str) -> dict:
    """Build the Composio v3 create-event args for a desired drive block.

    The block is created in its local timezone (`desired.timezone`) so it shows
    the correct local time; the unified codec description carries the leg identity
    + machine state for round-trip.

    The block is Busy (`transparency: "opaque"`): a drive is time the operator is
    physically unavailable, so scheduling tools must not book over it. Only
    `GOOGLECALENDAR_CREATE_EVENT` accepts `transparency` — `PATCH_EVENT` carries no
    such param (verified against the live toolkit), so the shift path cannot change
    it and a block's busy-ness is fixed at create time.

    `exclude_organizer: true` keeps Composio from injecting the connected user as
    a `needsAction` self-attendee — otherwise a personal drive block renders as an
    unconfirmed invite the operator must RSVP to. With it, the block has no
    attendees and shows as a plain accepted event (#158, verified against the live
    Composio toolkit). A distinct `colorId` is NOT set here: no Composio Google
    Calendar action (create/update/patch) exposes an event color field, so the
    colour half of #158 is deferred to the post-Composio calendar backend.
    """
    total_minutes = max(round((desired.end - desired.start).total_seconds() / 60), 1)
    local_start, tz_name = _start_in_local(desired.start, desired.timezone)
    return {
        "calendar_id": calendar_id,
        "summary": desired.summary,
        "start_datetime": local_start.isoformat(),
        "event_duration_hour": total_minutes // 60,
        "event_duration_minutes": total_minutes % 60,
        "location": desired.destination,
        "description": _desired_description(desired),
        "timezone": tz_name,
        "transparency": "opaque",
        "exclude_organizer": True,
    }


def _desired_description(desired: DesiredBlock) -> str:
    """The unified-codec description for a desired leg (identity + machine state)."""
    return build_description(
        summary=desired.summary,
        identity=desired.identity,
        kind=desired.kind,
        baseline_seconds=desired.baseline_seconds,
        anchor=desired.anchor,
        origin=desired.origin,
        destination=desired.destination,
        window_end=desired.window_end,
    )


def build_patch_args(desired: DesiredBlock, *, event_id: str, calendar_id: str) -> dict:
    """Build `GOOGLECALENDAR_PATCH_EVENT` args to shift an existing block IN PLACE.

    An update is a single atomic patch of the same event — new start/end (the
    leave-by moved), refreshed description + location — never a recreate-then-
    delete. So a sweep killed mid-write can no longer leave the new block next to
    an undeleted old one: the duplicate storm's mechanism is gone (#164). `end_time`
    is expressed in the same local zone as `start_time` so Composio re-reads both
    wall-clocks in `timezone` and the block lands at the right instant (#83).
    """
    local_start, tz_name = _start_in_local(desired.start, desired.timezone)
    local_end, _ = _start_in_local(desired.end, desired.timezone)
    return {
        "calendar_id": calendar_id,
        "event_id": event_id,
        "summary": desired.summary,
        "start_time": local_start.isoformat(),
        "end_time": local_end.isoformat(),
        "location": desired.destination,
        "description": _desired_description(desired),
        "timezone": tz_name,
    }


def _created_id(created: object) -> str | None:
    """Extract the new event id from a Composio create response.

    Delegates to flight-assist's `_created_event_id`, the live-verified extractor
    that handles Composio's real (nested `data.response_data.id`) shape — a
    hand-rolled subset would miss it and leave a rollback unable to delete the
    replacement, so both writers use the same extractor.
    """
    return _created_event_id(created)


def _delete(composio, *, calendar_id: str, event_id: str | None, result: ApplyResult) -> bool:
    """Delete one event. A 404 (already gone) counts as done. Returns success."""
    if event_id is None:
        return False
    try:
        composio.delete_event({"calendar_id": calendar_id, "event_id": event_id})
    except _WRITE_ERRORS as exc:
        if getattr(exc, "status_code", None) == 404:
            return True
        result.errors.append(f"delete {event_id}: {exc}")
        return False
    return True


def apply_plan(
    plan: ReconcilePlan,
    *,
    composio,
    calendar_id: str = "primary",
    budget_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ApplyResult:
    """Execute a reconcile plan against the calendar. Returns applied counts.

    Order: deletes (cleanup — drains any duplicate/orphan backlog), then creates,
    converts, updates.

    Updates are an in-place PATCH of the same event, so an update can never leave
    a duplicate even if the sweep is killed the instant after — the recreate-then-
    delete that produced the #164 storm is gone. Converts (adopting a legacy event
    into a unified one — two distinct events) still create-then-delete atomically.

    `budget_seconds` bounds the wall-clock spent on writes so the sweep returns a
    clean payload instead of being killed mid-write past the host precheck timeout
    (#164). Each write UNIT is atomic — the budget is checked BEFORE starting one,
    never mid-unit — so bounding never splits a create/patch/convert. Ops not
    started this sweep are counted in `deferred` and drained on the next sweep;
    because the reconcile is idempotent (matches existing blocks), resuming never
    duplicates. `monotonic` is injected for deterministic tests.
    """
    result = ApplyResult()
    deadline = monotonic() + budget_seconds if budget_seconds is not None else None

    def over_budget() -> bool:
        return deadline is not None and monotonic() >= deadline

    for d in plan.deletes:
        if over_budget():
            result.deferred += 1
            continue
        if _delete(composio, calendar_id=calendar_id, event_id=d.event_id, result=result):
            result.deleted += 1

    for c in plan.creates:
        if over_budget():
            result.deferred += 1
            continue
        try:
            composio.create_event(build_create_args(c.desired, calendar_id=calendar_id))
        except _WRITE_ERRORS as exc:
            result.errors.append(f"create {c.desired.identity}: {exc}")
            continue
        result.created += 1
        # Notify only for MEETING drives (skippable). Airport drives to/from a
        # flight are not skippable, so they are created silently.
        if c.desired.kind.startswith("meeting_"):
            result.added_meeting_legs.append(
                {
                    "identity": c.desired.identity,
                    "meeting": _meeting_name(c.desired.summary),
                    "when": _local_when(c.desired),
                    "anchor": c.desired.anchor.isoformat(),
                }
            )

    for cv in plan.converts:
        if over_budget():
            result.deferred += 1
            continue
        try:
            created = composio.create_event(build_create_args(cv.desired, calendar_id=calendar_id))
        except _WRITE_ERRORS as exc:
            result.errors.append(f"convert-create {cv.desired.identity}: {exc}")
            continue
        # Delete EVERY legacy event (list comp — no short-circuit, so a mid-list
        # failure doesn't skip the rest). Only count the convert when all legacy
        # blocks are gone; if any survived, the new block would duplicate it, so
        # roll the replacement back (same treatment as a failed update delete).
        deletes_ok = [
            _delete(composio, calendar_id=calendar_id, event_id=event_id, result=result)
            for event_id in cv.legacy_event_ids
        ]
        if all(deletes_ok):
            result.converted += 1
        else:
            new_id = _created_id(created)
            _delete(composio, calendar_id=calendar_id, event_id=new_id, result=result)

    for u in plan.updates:
        if over_budget():
            result.deferred += 1
            continue
        if u.event_id is None:
            # A matched block with no parseable event id can't be patched; the
            # reconcile only pairs an Update to a real fetched block, so this is
            # a defensive guard, logged and left for the next sweep.
            result.errors.append(f"update {u.desired.identity}: no event_id to patch")
            continue
        try:
            composio.patch_event(
                build_patch_args(u.desired, event_id=u.event_id, calendar_id=calendar_id)
            )
        except _WRITE_ERRORS as exc:
            result.errors.append(f"update-patch {u.desired.identity}: {exc}")
            continue
        result.updated += 1
        # Alert only on a MATERIAL drive-time change (traffic swing worth acting
        # on); routine sub-threshold re-times patch silently.
        delta = material_update_delta(u.prior_baseline_seconds, u.desired.baseline_seconds)
        if delta is not None:
            minutes, direction = delta
            result.material_updates.append(
                {
                    "identity": u.desired.identity,
                    "meeting": _meeting_name(u.desired.summary),
                    "minutes": minutes,
                    "direction": direction,
                    "when": _local_when(u.desired),
                    "anchor": u.desired.anchor.isoformat(),
                }
            )

    return result
