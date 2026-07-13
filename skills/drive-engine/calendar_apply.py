"""Apply a reconcile plan to the calendar — the engine's WRITE path.

Executes a `ReconcilePlan` against a Composio calendar client: deletes orphans,
creates new drive blocks, converts prior-gen blocks (create new + delete legacy),
and shifts changed blocks (recreate-then-delete). This is what makes the engine
authoritative instead of merely observant.

Atomicity mirrors the proven flight-assist pattern: a shift/convert always CREATES
the replacement first, then deletes the old — so a transient create failure raises
before any delete and leaves the prior block intact; a delete that 404s (already
gone) is an idempotent success; a real delete failure after a successful create
rolls the replacement back so no duplicate is left behind.

Deletes need only an event id, so the 44-block drive-planner cleanup runs through
the delete path with no create/timezone concerns. The create path builds the v3
flat `start_datetime` + duration args and carries the unified codec description so
the block round-trips.
"""

from __future__ import annotations

import sys
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import build_description  # noqa: E402
from reconcile import DesiredBlock, ReconcilePlan  # noqa: E402

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
    errors: list[str] = field(default_factory=list)

    @property
    def total_writes(self) -> int:
        return self.created + self.updated + self.deleted + self.converted


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

    `exclude_organizer: true` keeps Composio from injecting the connected user as
    a `needsAction` self-attendee — otherwise a personal drive block renders as an
    unconfirmed invite the operator must RSVP to. With it, the block has no
    attendees and shows as a plain accepted event (#158, verified against the live
    Composio toolkit). A distinct `colorId` is NOT set here: no Composio Google
    Calendar action (create/update/patch) exposes an event color field, so the
    colour half of #158 is deferred to the post-Composio calendar backend.
    """
    total_minutes = max(round((desired.end - desired.start).total_seconds() / 60), 1)
    description = build_description(
        summary=desired.summary,
        identity=desired.identity,
        kind=desired.kind,
        baseline_seconds=desired.baseline_seconds,
        anchor=desired.anchor,
        origin=desired.origin,
        destination=desired.destination,
        window_end=desired.window_end,
    )
    local_start, tz_name = _start_in_local(desired.start, desired.timezone)
    return {
        "calendar_id": calendar_id,
        "summary": desired.summary,
        "start_datetime": local_start.isoformat(),
        "event_duration_hour": total_minutes // 60,
        "event_duration_minutes": total_minutes % 60,
        "location": desired.destination,
        "description": description,
        "timezone": tz_name,
        "transparency": "transparent",
        "exclude_organizer": True,
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


def apply_plan(plan: ReconcilePlan, *, composio, calendar_id: str = "primary") -> ApplyResult:
    """Execute a reconcile plan against the calendar. Returns applied counts.

    Order: deletes (cleanup) first, then creates, converts, and updates. Each
    convert/update creates the replacement before deleting the old so a failure
    never leaves a gap; a failed post-create delete rolls the replacement back.
    """
    result = ApplyResult()

    for d in plan.deletes:
        if _delete(composio, calendar_id=calendar_id, event_id=d.event_id, result=result):
            result.deleted += 1

    for c in plan.creates:
        try:
            composio.create_event(build_create_args(c.desired, calendar_id=calendar_id))
        except _WRITE_ERRORS as exc:
            result.errors.append(f"create {c.desired.identity}: {exc}")
            continue
        result.created += 1

    for cv in plan.converts:
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
        try:
            created = composio.create_event(build_create_args(u.desired, calendar_id=calendar_id))
        except _WRITE_ERRORS as exc:
            result.errors.append(f"update-create {u.desired.identity}: {exc}")
            continue
        if _delete(composio, calendar_id=calendar_id, event_id=u.event_id, result=result):
            result.updated += 1
        else:
            # Old block survived; roll back the replacement so no duplicate remains.
            new_id = _created_id(created)
            _delete(composio, calendar_id=calendar_id, event_id=new_id, result=result)

    return result
