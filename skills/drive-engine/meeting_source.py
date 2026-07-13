"""Meeting-leg source — genuine ground meetings → desired drive blocks — pure.

The unified engine's meeting half (#156 leg table, `meeting` row). It consumes the
proven drive-planner `scan` classification (virtual/declined/past/skip filtering,
identity-based flight exclusion, per-meeting anchor resolution) and turns each
actionable meeting's legs into unified `DesiredBlock`s that feed the SAME reconcile
as the airport legs — so one engine owns both, no two-engine collision.

Two things make these correct where drive-planner's blocks went wrong:

- **Travel-awareness.** The origin comes from `position_at` (via the scan's
  `anchor_for`), so on a trip it resolves to where the operator actually is. A leg
  whose routed drive is implausibly long — the operator is abroad while the meeting
  is at home — is SUPPRESSED rather than invented. This is what stops "drive to
  Tennessee swim practice" appearing while the operator is in Europe.
- **Local timezone.** Each block carries the meeting's IANA tz so it renders at the
  correct local time instead of a foreign offset.

Pure: the caller runs the scan (I/O: calendar fetch) and supplies a `route` fn; a
route failure or unresolved anchor skips that leg with a diagnostic, never a block.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import GEN_LEGACY_DP, parse_block  # noqa: E402
from reconcile import DesiredBlock  # noqa: E402

# A drive block's human summary always starts with this.
_DRIVE_SUMMARY_PREFIX = "Drive:"


def exclude_drive_block_events(events: list[dict]) -> list[dict]:
    """Filter the meeting scan input (#156 — calendar-as-output), keeping dp blocks.

    Drops the drive blocks the reused `scan` CANNOT classify — the engine's own
    unified (dengine) blocks and legacy flight-assist (fadrive) airport blocks —
    so a later sweep never treats one as a meeting and plans a drive TO the drive
    block (self-referential duplicate).

    KEEPS legacy drive-planner (dp) blocks: `scan` recognizes those and uses them
    to bucket a meeting as already-handled (`has_block`). Hiding them would make
    `scan` re-plan that meeting and the engine would create a dengine block on top
    of the existing dp block — a duplicate. A `Drive:`-prefixed block with an
    unreadable marker is dropped too (it can't be a genuine meeting).
    """
    kept: list[dict] = []
    for event in events:
        parsed = parse_block(event)
        if parsed is not None:
            if parsed.generation == GEN_LEGACY_DP:
                kept.append(event)  # scan needs dp blocks for has_block detection
            continue  # dengine / fadrive — scan can't classify them; drop
        summary = event.get("summary") if isinstance(event, dict) else None
        if isinstance(summary, str) and summary.strip().startswith(_DRIVE_SUMMARY_PREFIX):
            continue  # a Drive: block with an unreadable marker — not a meeting
        kept.append(event)
    return kept


RouteFn = Callable[[str, str], "timedelta | None"]

# A routed meeting drive longer than this is implausible as a "drive to a meeting"
# — the operator almost certainly is not positioned to drive it (they flew, or are
# on a trip elsewhere). Mirrors drive-planner's MAX_REASONABLE_DRIVE_SECONDS.
DEFAULT_MAX_REASONABLE_DRIVE = timedelta(hours=3)

# scan directions → unified meeting leg kinds (distinct so reconcile keys uniquely).
_DIRECTION_KIND = {
    "outbound": "meeting_outbound",
    "bridge": "meeting_outbound",
    "return": "meeting_return",
}


def meeting_desired_blocks(
    meetings: list,
    *,
    route: RouteFn,
    max_reasonable_drive: timedelta = DEFAULT_MAX_REASONABLE_DRIVE,
) -> tuple[list[DesiredBlock], list[str]]:
    """Turn scan `MeetingClass` results into unified meeting `DesiredBlock`s.

    Only actionable meetings carry legs, so pass the scan output directly. Returns
    `(blocks, skipped_diagnostics)`. A leg is skipped (never blindly blocked) when
    its anchor is unresolved, its route fails, or its routed drive exceeds
    `max_reasonable_drive` (the travel-away suppression).
    """
    blocks: list[DesiredBlock] = []
    skipped: list[str] = []

    for meeting in meetings:
        for leg in meeting.legs:
            tag = f"meeting {meeting.meeting_id} {leg.direction}"
            if leg.origin is None or leg.destination is None:
                note = leg.anchor_note or "unresolved anchor"
                skipped.append(f"{tag}: {note}")
                continue
            drive = route(leg.origin, leg.destination)
            if drive is None:
                skipped.append(f"{tag}: route failed")
                continue
            if drive > max_reasonable_drive:
                minutes = int(drive.total_seconds() // 60)
                skipped.append(f"{tag}: {minutes}min drive implausible — suppressed (away?)")
                continue

            # Bridge leg: the drive must fit the gap between two consecutive
            # meetings. A drive longer than the gap can't be made (the "5h drive
            # inside a 45-min gap" case, #85) — suppress rather than create it.
            gap = getattr(leg, "gap_seconds", None)
            if isinstance(gap, int) and drive.total_seconds() > gap:
                skipped.append(
                    f"{tag}: {int(drive.total_seconds() // 60)}min drive exceeds the "
                    f"{gap // 60}min gap — suppressed"
                )
                continue

            kind = _DIRECTION_KIND.get(leg.direction)
            if kind is None:
                skipped.append(f"{tag}: unknown direction")
                continue

            if leg.direction == "return":
                depart_after: datetime | None = leg.depart_after
                if depart_after is None:
                    skipped.append(f"{tag}: return leg missing depart_after")
                    continue
                start, end, anchor = depart_after, depart_after + drive, depart_after
            else:
                arrive_by: datetime | None = leg.arrive_by
                if arrive_by is None:
                    skipped.append(f"{tag}: arrival leg missing arrive_by")
                    continue
                start, end, anchor = arrive_by - drive, arrive_by, arrive_by

            blocks.append(
                DesiredBlock(
                    identity=meeting.meeting_id,
                    kind=kind,
                    summary=f"Drive: {meeting.summary}",
                    start=start,
                    end=end,
                    origin=leg.origin,
                    destination=leg.destination,
                    baseline_seconds=int(drive.total_seconds()),
                    anchor=anchor,
                    timezone=getattr(meeting, "timezone", None),
                    legacy_keys=frozenset({(GEN_LEGACY_DP, meeting.meeting_id, leg.direction)}),
                )
            )

    return blocks, skipped
