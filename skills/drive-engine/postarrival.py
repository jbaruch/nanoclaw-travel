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

from block_codec import GEN_LEGACY_DP  # noqa: E402
from reconcile import DesiredBlock, HeldLeg  # noqa: E402
from scan import DEFAULT_TIGHT_GAP_SECONDS  # noqa: E402

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


def compose_postarrival(
    *,
    guards: Sequence[FlightGuard],
    meetings: Sequence[PostarrivalMeeting],
    desired: Sequence[DesiredBlock],
    held: Sequence[HeldLeg],
    lodging_lookup: AddressLookup,
    fallback_lookup: AddressLookup,
    route: RouteFn,
    max_handoff_drive: timedelta = _MAX_HANDOFF_DRIVE,
) -> tuple[list[DesiredBlock], list[HeldLeg], list[str]]:
    """Compose airport and meeting desired blocks around inbound arrivals.

    Returns `(desired, held, diagnostics)`. Unsafe meeting keys are removed from
    both desired and held so reconcile deletes a stale airborne or centroid block
    instead of preserving it under a provider-failure hold.
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
        if len(candidates) > 1:
            diagnostics.append(
                f"postarrival {guard.identity}: {len(candidates)} overlapping meetings; "
                "no automatic choice"
            )
            continue
        if not candidates:
            continue
        meeting = candidates[0]
        desired_list, extra_diags = _apply_handoff(
            guard=guard,
            meeting=meeting,
            desired=desired_list,
            unsafe=unsafe,
            lodging_lookup=lodging_lookup,
            fallback_lookup=fallback_lookup,
            route=route,
            max_handoff_drive=max_handoff_drive,
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

    # Held-only meeting legs (in-flight `past` bucket) never appear as desired
    # blocks. Mark a held key unsafe when its timestamp falls in a protected
    # interval, but leave meetings the handoff already rewrote — those own the
    # post-arrival return, and their stale outbound is already unsafe.
    for item in held:
        if item.identity in used_meetings or item.kind not in _MEETING_KINDS:
            continue
        if item.when is None or not _in_protected(item.when, guards):
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
    desired: list[DesiredBlock],
    unsafe: set[tuple[str, str]],
    lodging_lookup: AddressLookup,
    fallback_lookup: AddressLookup,
    route: RouteFn,
    max_handoff_drive: timedelta,
) -> tuple[list[DesiredBlock], list[str]]:
    venue = meeting.location
    assert venue is not None
    diagnostics: list[str] = []
    drive = route(guard.airport_point, venue)
    if drive is None or drive > max_handoff_drive:
        diagnostics.append(
            f"postarrival {meeting.identity}: airport-to-venue route failed"
            if drive is None
            else f"postarrival {meeting.identity}: airport-to-venue drive exceeds cap"
        )
        return _drop_meeting(desired, unsafe, meeting.identity), diagnostics
    if guard.ready_at + drive >= meeting.end:
        diagnostics.append(
            f"postarrival {meeting.identity}: not reachable from {guard.identity} "
            f"by {guard.ready_at.isoformat()}"
        )
        return _drop_meeting(desired, unsafe, meeting.identity), diagnostics

    lodging = lodging_lookup(guard.ready_at)
    return_dest = lodging if lodging is not None else fallback_lookup(guard.ready_at)
    need_return = not meeting.return_suppressed
    ret_drive = route(venue, return_dest) if need_return and return_dest is not None else None
    if need_return and (ret_drive is None or return_dest is None):
        diagnostics.append(f"postarrival {meeting.identity}: venue-to-lodging route failed")
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
