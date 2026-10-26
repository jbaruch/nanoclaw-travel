"""Post-arrival meeting composition — flight-aware handoff before reconcile.

A meeting still going after landing replaces airport-to-lodging with
airport-to-venue; the return goes to lodging. No drive may begin inside a
flight's clearance / flight / post-arrival interval. Ended or unreachable
meetings get no drive. Pure: no I/O, no clock.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import GEN_LEGACY_DP, GEN_UNIFIED, ParsedBlock  # noqa: E402
from meeting_source import DEFAULT_MAX_REASONABLE_DRIVE  # noqa: E402
from reconcile import DesiredBlock, HeldLeg  # noqa: E402
from scan import DEFAULT_TIGHT_GAP_SECONDS, same_place  # noqa: E402

# Matches engine.MAX_AIRPORT_DRIVE: a longer airport-to-venue is not a real drive.
_MAX_HANDOFF_DRIVE = timedelta(hours=6)

_MEETING_KINDS = frozenset({"meeting_outbound", "meeting_return"})

# Scan buckets that represent a real in-person meeting the composer may hand off.
# filtered / skipped stay out; past is included because an in-flight sweep sees
# an already-started meeting as past (scan assigns that after declined/cancelled/
# virtual/anchor filters, and before the skip check).
_ELIGIBLE_BUCKETS = frozenset({"needs_decision", "bridge", "back_to_back", "has_block", "past"})

RouteFn = Callable[[str, str], "timedelta | None"]
AddressLookup = Callable[[datetime], str | None]


@dataclass(frozen=True)
class FlightGuard:
    """Protected bounds and arrival routing for one flight."""

    identity: str
    guard_start: datetime
    ready_at: datetime
    airport_code: str
    airport_point: str
    airport_label: str
    timezone: str | None = None
    chain_closing: bool = False


@dataclass(frozen=True)
class PostarrivalMeeting:
    """Meeting intent taken from a scan `MeetingClass`, without re-parsing events."""

    identity: str
    summary: str
    location: str | None
    start: datetime
    end: datetime
    timezone: str | None = None
    return_suppressed: bool = False
    driving_to: bool = False


def _skip_is_active(skip_state: dict[str, str] | None, identity: str, now: datetime | None) -> bool:
    """True when `skip_state` still suppresses `identity` at `now`.

    Mirrors scan's skip predicate so a `past` meeting (classified before the
    skip check) cannot enter the composer while an active skip remains.
    """
    if not skip_state:
        return False
    expiry_raw = skip_state.get(identity)
    if not isinstance(expiry_raw, str):
        return False
    text = expiry_raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        expiry = datetime.fromisoformat(text)
    except ValueError:
        return False
    if expiry.tzinfo is None:
        return False
    if now is None:
        return True
    return expiry > now


def _has_return_leg(meeting: object) -> bool:
    legs = getattr(meeting, "legs", ()) or ()
    return any(getattr(leg, "direction", None) == "return" for leg in legs)


def _has_any_leg(meeting: object) -> bool:
    legs = getattr(meeting, "legs", ()) or ()
    return bool(legs)


def _next_starts_within(meeting: object, others: Sequence[object], tight_gap_seconds: int) -> bool:
    end = getattr(meeting, "end", None)
    identity = getattr(meeting, "meeting_id", None)
    if end is None:
        return False
    for other in others:
        if getattr(other, "meeting_id", None) == identity:
            continue
        start = getattr(other, "start", None)
        if start is None:
            continue
        gap = (start - end).total_seconds()
        if 0 <= gap <= tight_gap_seconds:
            return True
    return False


def postarrival_meetings_from(
    scan_results: Sequence[object],
    *,
    skip_state: dict[str, str] | None = None,
    now: datetime | None = None,
    tight_gap_seconds: int = DEFAULT_TIGHT_GAP_SECONDS,
    driving_to: frozenset[str] = frozenset(),
) -> list[PostarrivalMeeting]:
    """Lift scan results that are eligible in-person meeting intents.

    Declined, cancelled, skipped, virtual, at-anchor, planner-block, and other
    filtered results stay out even when they still carry a timed location. An
    active skip excludes a `past` meeting (scan buckets past before it checks
    skip). A legitimate in-flight `past` in-person meeting remains eligible.
    """
    eligible: list[object] = []
    for meeting in scan_results:
        identity = getattr(meeting, "meeting_id", None)
        start = getattr(meeting, "start", None)
        end = getattr(meeting, "end", None)
        bucket = getattr(meeting, "bucket", None)
        if not isinstance(identity, str) or start is None or end is None:
            continue
        if bucket not in _ELIGIBLE_BUCKETS:
            continue
        if _skip_is_active(skip_state, identity, now):
            continue
        eligible.append(meeting)

    out: list[PostarrivalMeeting] = []
    for meeting in eligible:
        identity = getattr(meeting, "meeting_id", None)
        start = getattr(meeting, "start", None)
        end = getattr(meeting, "end", None)
        location = getattr(meeting, "location", None)
        if not isinstance(identity, str) or start is None or end is None:
            continue
        if _has_any_leg(meeting):
            return_suppressed = not _has_return_leg(meeting)
        else:
            return_suppressed = _next_starts_within(meeting, eligible, tight_gap_seconds)
        out.append(
            PostarrivalMeeting(
                identity=identity,
                summary=str(getattr(meeting, "summary", "") or ""),
                location=location if isinstance(location, str) and location.strip() else None,
                start=start,
                end=end,
                timezone=getattr(meeting, "timezone", None),
                return_suppressed=return_suppressed,
                driving_to=identity in driving_to,
            )
        )
    return out


def _key(block: DesiredBlock) -> tuple[str, str]:
    return (block.identity, block.kind)


def _in_protected(start: datetime, guards: Sequence[FlightGuard]) -> bool:
    return any(guard.guard_start <= start < guard.ready_at for guard in guards)


def _meeting_keys(identity: str) -> tuple[tuple[str, str], tuple[str, str]]:
    return ((identity, "meeting_outbound"), (identity, "meeting_return"))


def _drop_meeting(
    desired: list[DesiredBlock],
    unsafe: set[tuple[str, str]],
    identity: str,
) -> list[DesiredBlock]:
    unsafe.update(_meeting_keys(identity))
    return [
        block
        for block in desired
        if not (block.kind in _MEETING_KINDS and block.identity == identity)
    ]


def _ongoing(meeting: PostarrivalMeeting, ready_at: datetime) -> bool:
    return meeting.start < ready_at < meeting.end


def _unreachable_note(
    guard: FlightGuard,
    meeting: PostarrivalMeeting,
    drive: timedelta | None,
    max_handoff_drive: timedelta,
) -> str | None:
    """Why `meeting` is proven unreachable from `guard`, or None.

    A failed route (`drive` None) proves nothing: reachability is unknown, so the
    candidate stays in play and the fail-safe applies later.
    """
    if drive is None:
        return None
    if drive > max_handoff_drive:
        return f"postarrival {meeting.identity}: airport-to-venue drive exceeds cap"
    if drive > DEFAULT_MAX_REASONABLE_DRIVE and not meeting.driving_to:
        return (
            f"postarrival {meeting.identity}: airport-to-venue drive "
            "implausible — suppressed (away?)"
        )
    if guard.ready_at + drive >= meeting.end:
        return (
            f"postarrival {meeting.identity}: not reachable from {guard.identity} "
            f"by {guard.ready_at.isoformat()}"
        )
    return None


def _remaining_continuations(
    meeting: PostarrivalMeeting,
    meetings: Sequence[PostarrivalMeeting],
    excluded: frozenset[str],
    tight_gap_seconds: int,
    desired: Sequence[DesiredBlock],
    held: Sequence[HeldLeg],
    current_blocks: Sequence[ParsedBlock],
) -> tuple[bool, set[tuple[str, str]]]:
    """Validate the actual surviving path after an unreachable neighbour is dropped.

    A chronological neighbour alone cannot suppress the hotel return. Its
    inbound must come from the surviving venue and fit the remaining gap, or
    both meetings must share that venue. A provider-failure hold counts only
    when the existing block proves the same valid continuation.
    """
    continuing = False
    invalid: set[tuple[str, str]] = set()
    held_keys = {item.key for item in held}
    for other in meetings:
        gap = (other.start - meeting.end).total_seconds()
        if (
            other.identity == meeting.identity
            or other.identity in excluded
            or other.location is None
            or not 0 <= gap <= tight_gap_seconds
        ):
            continue
        if same_place(meeting.location, other.location):
            continuing = True
            continue
        key = (other.identity, "meeting_outbound")
        planned = any(
            _key(block) == key
            and same_place(block.origin, meeting.location)
            and same_place(block.destination, other.location)
            and meeting.end <= block.start <= block.end <= other.start
            for block in desired
        )
        retained = key in held_keys and any(
            block.generation == GEN_UNIFIED
            and (block.identity, block.kind) == key
            and same_place(block.origin, meeting.location)
            and same_place(block.destination, other.location)
            and block.anchor is not None
            and block.baseline_seconds is not None
            and block.baseline_seconds >= 0
            and meeting.end <= block.anchor - timedelta(seconds=block.baseline_seconds)
            and block.anchor <= other.start
            for block in current_blocks
        )
        if planned or retained:
            continuing = True
        else:
            invalid.add(key)
    return continuing, invalid


def _current_drive_start(block: ParsedBlock) -> datetime | None:
    """When an existing unified meeting block's drive begins, or None if unknown.

    A return block's anchor is its departure. An outbound block's anchor is the
    arrival deadline, so its drive began `baseline_seconds` earlier.
    """
    if block.anchor is None:
        return None
    if block.kind == "meeting_return":
        return block.anchor
    if block.kind == "meeting_outbound" and block.baseline_seconds is not None:
        return block.anchor - timedelta(seconds=block.baseline_seconds)
    return None


def _held_is_unsafe(
    item: HeldLeg,
    current_by_key: dict[tuple[str, str], list[ParsedBlock]],
    guards: Sequence[FlightGuard],
) -> bool:
    """Whether the drive a held meeting leg keeps alive begins inside a guard.

    A hold keeps an EXISTING block, so the block's own drive start decides. A
    return hold's `when` is also its drive start. An outbound hold's `when` is the
    arrival deadline, never the drive start: a safe outbound that leaves before
    `guard_start` may arrive inside the guard, and a failed route must not delete
    it. An outbound hold with no readable block has no known drive start.
    """
    starts = [
        start
        for block in current_by_key.get(item.key, ())
        if (start := _current_drive_start(block)) is not None
    ]
    if item.kind == "meeting_return" and item.when is not None:
        starts.append(item.when)
    return any(_in_protected(start, guards) for start in starts)


def compose_postarrival(
    *,
    guards: Sequence[FlightGuard],
    meetings: Sequence[PostarrivalMeeting],
    desired: Sequence[DesiredBlock],
    held: Sequence[HeldLeg],
    lodging_lookup: AddressLookup,
    fallback_lookup: AddressLookup,
    route: RouteFn,
    current_blocks: Sequence[ParsedBlock] = (),
    max_handoff_drive: timedelta = _MAX_HANDOFF_DRIVE,
) -> tuple[list[DesiredBlock], list[HeldLeg], list[str]]:
    """Compose airport and meeting desired blocks around inbound arrivals.

    Returns `(desired, held, diagnostics)`. Unsafe meeting keys are removed from
    both desired and held so reconcile deletes a stale airborne or centroid block
    instead of preserving it under a provider-failure hold. `current_blocks` are the
    calendar's existing blocks: a held leg is judged by the drive start of the
    block it keeps alive.
    """
    desired_list = list(desired)
    diagnostics: list[str] = []
    unsafe: set[tuple[str, str]] = set()
    used_meetings: set[str] = set()
    arrival_ids = {block.identity for block in desired_list if block.kind == "airport_arrival"}

    for guard in guards:
        if guard.identity not in arrival_ids:
            continue
        candidates = [
            meeting
            for meeting in meetings
            if meeting.identity not in used_meetings
            and meeting.location is not None
            and _ongoing(meeting, guard.ready_at)
        ]
        # Proven-unreachable candidates (ended, too far) take the unreachable path
        # and never count toward ambiguity. A failed route is unknown and stays.
        in_play: list[tuple[PostarrivalMeeting, timedelta | None]] = []
        dropped: set[str] = set()
        for meeting in candidates:
            assert meeting.location is not None
            drive = route(guard.airport_point, meeting.location)
            note = _unreachable_note(guard, meeting, drive, max_handoff_drive)
            if note is not None:
                diagnostics.append(note)
                desired_list = _drop_meeting(desired_list, unsafe, meeting.identity)
                used_meetings.add(meeting.identity)
                dropped.add(meeting.identity)
                continue
            in_play.append((meeting, drive))
        if len(in_play) > 1:
            diagnostics.append(
                f"postarrival {guard.identity}: {len(in_play)} overlapping meetings; "
                "no automatic choice"
            )
            continue
        if not in_play:
            continue
        meeting, drive = in_play[0]
        if meeting.return_suppressed and dropped:
            # Scan may have routed a later meeting from the dropped neighbour.
            # Keep suppression only when a surviving continuation is usable.
            continuing, invalid = _remaining_continuations(
                meeting,
                meetings,
                frozenset(dropped),
                DEFAULT_TIGHT_GAP_SECONDS,
                desired_list,
                held,
                current_blocks,
            )
            unsafe.update(invalid)
            desired_list = [block for block in desired_list if _key(block) not in invalid]
            meeting = replace(meeting, return_suppressed=continuing)
        desired_list, extra_diags = _apply_handoff(
            guard=guard,
            meeting=meeting,
            drive=drive,
            desired=desired_list,
            unsafe=unsafe,
            lodging_lookup=lodging_lookup,
            fallback_lookup=fallback_lookup,
            route=route,
        )
        diagnostics.extend(extra_diags)
        used_meetings.add(meeting.identity)

    kept: list[DesiredBlock] = []
    protected_outbound: set[str] = set()
    protected_return: set[str] = set()
    for block in desired_list:
        if not _in_protected(block.start, guards):
            kept.append(block)
            continue
        unsafe.add(_key(block))
        if block.kind == "meeting_outbound":
            protected_outbound.add(block.identity)
            diagnostics.append(
                f"postarrival {block.identity}: drive would start inside a flight "
                "interval — suppressed"
            )
        elif block.kind == "meeting_return":
            protected_return.add(block.identity)
            diagnostics.append(
                f"postarrival {block.identity}: drive would start inside a flight "
                "interval — suppressed"
            )

    # A held meeting leg (in-flight `past` bucket, or a failed route) keeps an
    # existing block alive. It is unsafe only when that block's drive begins inside
    # a protected interval; meetings the handoff already rewrote own their
    # post-arrival return and their stale outbound is already unsafe.
    current_by_key: dict[tuple[str, str], list[ParsedBlock]] = {}
    for block in current_blocks:
        if block.generation == GEN_UNIFIED and block.identity is not None and block.kind:
            current_by_key.setdefault((block.identity, block.kind), []).append(block)
    for item in held:
        if item.identity in used_meetings or item.kind not in _MEETING_KINDS:
            continue
        if not _held_is_unsafe(item, current_by_key, guards):
            continue
        if item.kind == "meeting_outbound":
            protected_outbound.add(item.identity)
        else:
            protected_return.add(item.identity)
        diagnostics.append(
            f"postarrival {item.identity}: drive would start inside a flight interval — suppressed"
        )

    for identity in protected_outbound:
        kept = _drop_meeting(kept, unsafe, identity)
    for identity in protected_return - protected_outbound:
        unsafe.add((identity, "meeting_return"))
        kept = [
            block
            for block in kept
            if not (block.identity == identity and block.kind == "meeting_return")
        ]

    held_list = [item for item in held if item.key not in unsafe]
    return kept, held_list, diagnostics


def _apply_handoff(
    *,
    guard: FlightGuard,
    meeting: PostarrivalMeeting,
    drive: timedelta | None,
    desired: list[DesiredBlock],
    unsafe: set[tuple[str, str]],
    lodging_lookup: AddressLookup,
    fallback_lookup: AddressLookup,
    route: RouteFn,
) -> tuple[list[DesiredBlock], list[str]]:
    """Rewrite the arrival to `meeting`; `drive` is its already-screened route."""
    venue = meeting.location
    assert venue is not None
    diagnostics: list[str] = []
    if drive is None:
        diagnostics.append(f"postarrival {meeting.identity}: airport-to-venue route failed")
        return _drop_meeting(desired, unsafe, meeting.identity), diagnostics

    lodging = lodging_lookup(guard.ready_at)
    return_dest = lodging if lodging is not None else fallback_lookup(guard.ready_at)
    need_return = not meeting.return_suppressed
    ret_drive = route(venue, return_dest) if need_return and return_dest is not None else None
    if need_return and (ret_drive is None or return_dest is None):
        diagnostics.append(f"postarrival {meeting.identity}: venue-to-lodging route failed")
        return _drop_meeting(desired, unsafe, meeting.identity), diagnostics

    if (
        ret_drive is not None
        and ret_drive > DEFAULT_MAX_REASONABLE_DRIVE
        and not meeting.driving_to
    ):
        diagnostics.append(
            f"postarrival {meeting.identity}: venue-to-lodging drive "
            "implausible — suppressed (away?)"
        )
        return _drop_meeting(desired, unsafe, meeting.identity), diagnostics

    rewritten: list[DesiredBlock] = []
    found_return = False
    for block in desired:
        if block.kind == "meeting_outbound" and block.identity == meeting.identity:
            unsafe.add(_key(block))
            continue
        if block.kind == "airport_arrival" and block.identity == guard.identity:
            rewritten.append(
                replace(
                    block,
                    summary=f"Drive: {guard.airport_code} → {venue}",
                    start=guard.ready_at,
                    end=guard.ready_at + drive,
                    origin=guard.airport_label,
                    destination=venue,
                    baseline_seconds=int(drive.total_seconds()),
                    anchor=guard.ready_at,
                )
            )
            continue
        if block.kind == "meeting_return" and block.identity == meeting.identity:
            found_return = True
            if not need_return:
                unsafe.add(_key(block))
                continue
            assert ret_drive is not None and return_dest is not None
            rewritten.append(
                replace(
                    block,
                    start=meeting.end,
                    end=meeting.end + ret_drive,
                    origin=venue,
                    destination=return_dest,
                    baseline_seconds=int(ret_drive.total_seconds()),
                    anchor=meeting.end,
                )
            )
            continue
        rewritten.append(block)

    if need_return and not found_return:
        assert ret_drive is not None and return_dest is not None
        rewritten.append(
            DesiredBlock(
                identity=meeting.identity,
                kind="meeting_return",
                summary=f"Drive: {meeting.summary}",
                start=meeting.end,
                end=meeting.end + ret_drive,
                origin=venue,
                destination=return_dest,
                baseline_seconds=int(ret_drive.total_seconds()),
                anchor=meeting.end,
                timezone=meeting.timezone,
                legacy_keys=frozenset({(GEN_LEGACY_DP, meeting.identity, "return")}),
            )
        )
    elif not need_return:
        unsafe.add((meeting.identity, "meeting_return"))

    unsafe.add((meeting.identity, "meeting_outbound"))
    return rewritten, diagnostics
