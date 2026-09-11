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
- **Display timezone.** Each block carries the operator's current IANA zone when
  the core `current-tz` reader has one (#301), the meeting's own zone otherwise —
  so the block and the operator notice read on the clock the operator is looking
  at, never a stale offset a source event dragged home from a trip.

Pure: the caller runs the scan (I/O: calendar fetch) and supplies a `route` fn; a
route failure or unresolved anchor skips that leg with a diagnostic, never a block.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import GEN_LEGACY_DP, parse_block  # noqa: E402
from reconcile import DesiredBlock  # noqa: E402
from scan import same_place  # noqa: E402

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


@dataclass(frozen=True)
class TripPresence:
    """Where the operator sleeps during a trip they have confirmed they drive to.

    `scan` resolves ONE anchor per meeting, at the event's start, and both of
    that meeting's legs use it. On a trip whose check-in is stamped after the
    first event that anchor is home, so the drive back from that event was
    planned to home — a 4-hour drive out of the middle of a trip, with the next
    morning's drive starting from a hotel nothing ever reached.

    The operator sleeps at the lodging from the first event of the trip until
    the last. So `lodging` replaces a home endpoint except where home is real:
    the drive OUT to the first event (they leave from home) and the drive BACK
    from the last (`lodging_source` plans the actual drive home).
    """

    lodging: str
    is_first: bool
    is_last: bool


def _trip_endpoints(leg, presence: TripPresence | None) -> tuple[str, str]:
    """A leg's endpoints with a mid-trip home rewritten to the trip's lodging.

    Off a driving trip, or where home is the real endpoint, the leg is returned
    unchanged. `leg.origin` / `leg.destination` are non-None at every call site.
    """
    origin, destination = leg.origin, leg.destination
    if presence is None:
        return origin, destination
    if leg.direction == "return":
        # Back from the LAST event, home is real — `lodging_source` plans that
        # drive, and this leg deferring to it keeps one owner for the way home.
        if not presence.is_last and destination != presence.lodging:
            destination = presence.lodging
    elif leg.direction == "outbound" and not presence.is_first:
        # Out to any event but the FIRST, the operator sets off from the room,
        # not the house they left days ago.
        #
        # `outbound` explicitly, never "not a return": a BRIDGE leg runs venue
        # to venue between two tight-gap meetings, and its origin is the prior
        # venue rather than the anchor. Rewriting that to the lodging invents a
        # detour through the hotel between back-to-back events (#243 review).
        if origin != presence.lodging:
            origin = presence.lodging
    return origin, destination


def meeting_desired_blocks(
    meetings: list,
    *,
    route: RouteFn,
    max_reasonable_drive: timedelta = DEFAULT_MAX_REASONABLE_DRIVE,
    driving_to: dict[str, TripPresence] | None = None,
    display_tz: str | None = None,
) -> tuple[list[DesiredBlock], list[str]]:
    """Turn scan `MeetingClass` results into unified meeting `DesiredBlock`s.

    Only actionable meetings carry legs, so pass the scan output directly. Returns
    `(blocks, skipped_diagnostics)`. A leg is skipped (never blindly blocked) when
    its anchor is unresolved, its route fails, or its routed drive exceeds
    `max_reasonable_drive` (the travel-away suppression).

    A leg whose origin IS its destination is skipped too (#301): the scan
    already filters a meeting held at the anchor, but the lodging rewrite below
    can collapse a leg the same way — a venue that is the trip's hotel — and a
    zero-length drive would still land as a degenerate one-minute block. The
    comparison is `scan.same_place`: the schedule's lodging address is only
    stripped, the scan's location is whitespace-collapsed, and either may
    differ in case.

    `driving_to` are the meeting ids the away-suppression must NOT fire on: the
    events of a flight-less trip the operator has confirmed they DRIVE to. The
    suppression reads a long drive as "not positioned to make it — they flew, or
    are elsewhere", and for those meetings that premise is known false. A
    check-in stamped after the trip's first event makes the anchor resolve home,
    so the getting-there drive routes home→venue at its true length and the cap
    deleted the very event the trip exists for (#242).

    For those same meetings a home endpoint is rewritten to the trip's lodging
    unless home is real there — see `TripPresence`.

    `display_tz` is the operator's current IANA zone from the core `current-tz`
    reader, or None when it has none. It is the zone every block is written in
    and the notice renders its "at HH:MM" from (#301). The instant is always the
    source event's; only the wall-clock it is shown on changes. A source event
    can carry a stale offset from a trip (`+02:00` on an `America/Chicago` event
    after Amsterdam), and the scan's offset-derived `Etc/GMT±N` fallback keeps
    that instant right while showing it on the wrong clock; the operator's zone
    is the clock Google Calendar shows them the same event on. None keeps the
    meeting's own zone.
    """
    presence = driving_to or {}
    blocks: list[DesiredBlock] = []
    skipped: list[str] = []

    for meeting in meetings:
        for leg in meeting.legs:
            tag = f"meeting {meeting.meeting_id} {leg.direction}"
            if leg.origin is None or leg.destination is None:
                note = leg.anchor_note or "unresolved anchor"
                skipped.append(f"{tag}: {note}")
                continue
            origin, destination = _trip_endpoints(leg, presence.get(meeting.meeting_id))
            if same_place(origin, destination):
                skipped.append(f"{tag}: origin is the destination — no drive")
                continue
            drive = route(origin, destination)
            if drive is None:
                skipped.append(f"{tag}: route failed")
                continue
            if drive > max_reasonable_drive and meeting.meeting_id not in presence:
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
                    origin=origin,
                    destination=destination,
                    baseline_seconds=int(drive.total_seconds()),
                    anchor=anchor,
                    timezone=display_tz or getattr(meeting, "timezone", None),
                    legacy_keys=frozenset({(GEN_LEGACY_DP, meeting.meeting_id, leg.direction)}),
                )
            )

    return blocks, skipped
