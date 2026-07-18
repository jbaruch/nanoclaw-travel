"""Apply a reconcile plan to the calendar — the engine's WRITE path.

Executes a `ReconcilePlan` against a Google Calendar client: deletes orphans,
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
path with no create/timezone concerns. The create path builds a native
events.insert body with nested start/end and carries the unified codec
description so the block round-trips.
"""

from __future__ import annotations

import sys
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import build_extended_properties  # noqa: E402
from reconcile import DesiredBlock, ReconcilePlan, material_update_delta  # noqa: E402

# A drive block's human summary is "Drive: <meeting/leg>". Stripping the prefix
# recovers the name the operator recognizes for a notification.
_DRIVE_SUMMARY_PREFIX = "Drive: "

# Every drive block is stamped Tangerine (Google Calendar event `colorId` "6") so
# it reads as visually distinct from meetings and flights (#167, owner decision
# 2026-07-12). Calendar event colorId is a string enum "1".."11"; "6" is the only
# orange. Set on both create and shift, so a block created before this recolors on
# its next patch rather than needing a backfill pass.
_DRIVE_BLOCK_COLOR_ID = "6"

# flight-assist ships GoogleCalendarError; resolve it cross-bundle for the caught type.
_FA = Path("/home/node/.claude/skills/tessl__flight-assist")
if not _FA.is_dir():
    _FA = _BUNDLE_DIR.parent / "flight-assist"
if str(_FA) not in sys.path:
    sys.path.insert(0, str(_FA))

from calendar_reconcile import _created_event_id  # noqa: E402
from google_calendar_client import GoogleCalendarError  # noqa: E402

_WRITE_ERRORS = (GoogleCalendarError, urllib.error.URLError)


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

    A meeting/airport block rendered in its local IANA tz shows the right local
    time on the calendar (an 08:45 Tennessee practice reads 08:45, not a foreign
    offset), and the zone name it returns is what `timeZone` declares. An
    unknown / missing tz falls back to unambiguous UTC — a real IANA name, so
    it is always safe to send as `timeZone`.
    """
    if tz_name:
        try:
            return start.astimezone(ZoneInfo(tz_name)), tz_name
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return start.astimezone(timezone.utc), "UTC"


def build_create_args(desired: DesiredBlock, *, calendar_id: str) -> dict:
    """Build the native events.insert body for a desired drive block.

    The block is rendered in its local timezone (`desired.timezone`) so it shows
    the correct local time. Machine state (leg identity + the fields the recheck
    reads back) rides in `extendedProperties.private`, a machine-only field, and
    the `description` carries only the operator-facing route line (#178 writer
    flip). It squatted in the description only because the retired Composio toolkit
    exposed no writable `extendedProperties`; the native API does, and the reader
    has read both since the dual-read step, so a block written this way round-trips
    and a still-description-carried block created before the flip is read by the
    fallback until it ages out.

    The block is Busy (`transparency: "opaque"`): a drive is time the operator is
    physically unavailable, so scheduling tools must not book over it. Calendar
    accepts `transparency` on patch as well as insert, but the shift path does not
    send it — a block's busy-ness stays fixed at create time, as it was under the
    toolkit that could not patch it at all.

    No self-attendee is injected: Calendar creates an event with no attendees, so
    the block shows as a plain accepted event rather than an unconfirmed invite
    the operator must RSVP to. That was #158's attendee half, and it needed an
    `exclude_organizer: true` flag to suppress under Composio; natively there is
    nothing to suppress. #158's colour half — a distinct `colorId` — ships here as
    `_DRIVE_BLOCK_COLOR_ID`: impossible under the old toolkit, native now (#167).
    """
    end = max(desired.end, desired.start + timedelta(minutes=1))
    local_start, tz_name = _start_in_local(desired.start, desired.timezone)
    local_end, _ = _start_in_local(end, desired.timezone)
    return {
        "calendar_id": calendar_id,
        "summary": desired.summary,
        "start": {"dateTime": local_start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": local_end.isoformat(), "timeZone": tz_name},
        "location": desired.destination,
        "description": _human_description(desired),
        "transparency": "opaque",
        "colorId": _DRIVE_BLOCK_COLOR_ID,
        "extendedProperties": _desired_extended_properties(desired),
    }


def _human_description(desired: DesiredBlock) -> str:
    """The operator-facing block description — the drive's route, `origin → dest`.

    Machine state no longer rides here (#178 writer flip): it moved to
    `extendedProperties.private`. The description now carries only what the
    operator reads in the calendar UI. The route complements the `summary` title
    (the meeting/flight name) and the `location` (the destination) with the one
    detail neither shows — where the drive starts.
    """
    return f"{desired.origin} → {desired.destination}"


def _desired_extended_properties(desired: DesiredBlock) -> dict:
    """The machine-state `extendedProperties` body for a desired leg (#178).

    Nested under the event's top-level `extendedProperties` key; Calendar merges
    the `private` map into the event's existing private properties on patch, so a
    shift re-asserts the state without clobbering a neighbour's tag.
    """
    return build_extended_properties(
        identity=desired.identity,
        kind=desired.kind,
        baseline_seconds=desired.baseline_seconds,
        anchor=desired.anchor,
        origin=desired.origin,
        destination=desired.destination,
        window_end=desired.window_end,
    )


def build_patch_args(desired: DesiredBlock, *, event_id: str, calendar_id: str) -> dict:
    """Build the native events.patch body to shift an existing block IN PLACE.

    An update is a single atomic patch of the same event — new start/end (the
    leave-by moved), refreshed description + location — never a recreate-then-
    delete. So a sweep killed mid-write can no longer leave the new block next to
    an undeleted old one: the duplicate storm's mechanism is gone (#164).

    The patch writes `extendedProperties` (the #178 machine-state home) and the
    human-only description, so a block still carrying its state in the description
    from before the writer flip is migrated to `extendedProperties` on its first
    post-flip shift — the marker + state comment leave the description as this
    replaces it. The patch also re-asserts `colorId` (#167) so a pre-colour block
    is recoloured Tangerine on the same shift.
    """
    end = max(desired.end, desired.start + timedelta(minutes=1))
    local_start, tz_name = _start_in_local(desired.start, desired.timezone)
    local_end, _ = _start_in_local(end, desired.timezone)
    return {
        "calendar_id": calendar_id,
        "event_id": event_id,
        "summary": desired.summary,
        "start": {"dateTime": local_start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": local_end.isoformat(), "timeZone": tz_name},
        "location": desired.destination,
        "description": _human_description(desired),
        "colorId": _DRIVE_BLOCK_COLOR_ID,
        "extendedProperties": _desired_extended_properties(desired),
    }


def _created_id(created: object) -> str | None:
    """Extract the new event id from an events.insert response.

    Delegates to flight-assist's `_created_event_id` so both writers read the
    create response through one extractor — a rollback that could not find the
    replacement's id would be unable to delete it.
    """
    return _created_event_id(created)


def _delete(calendar, *, calendar_id: str, event_id: str | None, result: ApplyResult) -> bool:
    """Delete one event. A 404 (already gone) counts as done. Returns success."""
    if event_id is None:
        return False
    try:
        calendar.delete_event({"calendar_id": calendar_id, "event_id": event_id})
    except _WRITE_ERRORS as exc:
        if getattr(exc, "status_code", None) == 404:
            return True
        result.errors.append(f"delete {event_id}: {exc}")
        return False
    return True


def apply_plan(
    plan: ReconcilePlan,
    *,
    calendar,
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
        if _delete(calendar, calendar_id=calendar_id, event_id=d.event_id, result=result):
            result.deleted += 1

    for c in plan.creates:
        if over_budget():
            result.deferred += 1
            continue
        try:
            calendar.create_event(build_create_args(c.desired, calendar_id=calendar_id))
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
            created = calendar.create_event(build_create_args(cv.desired, calendar_id=calendar_id))
        except _WRITE_ERRORS as exc:
            result.errors.append(f"convert-create {cv.desired.identity}: {exc}")
            continue
        # Delete EVERY legacy event (list comp — no short-circuit, so a mid-list
        # failure doesn't skip the rest). Only count the convert when all legacy
        # blocks are gone; if any survived, the new block would duplicate it, so
        # roll the replacement back (same treatment as a failed update delete).
        deletes_ok = [
            _delete(calendar, calendar_id=calendar_id, event_id=event_id, result=result)
            for event_id in cv.legacy_event_ids
        ]
        if all(deletes_ok):
            result.converted += 1
        else:
            new_id = _created_id(created)
            _delete(calendar, calendar_id=calendar_id, event_id=new_id, result=result)

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
            calendar.patch_event(
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
