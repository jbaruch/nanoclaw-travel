"""Classify calendar events into drive-planner transit buckets — the brain.

drive-planner's job is "for every in-person ground meeting, make sure a
traffic-aware drive block exists (or was deliberately skipped), and never
nag about one that doesn't need it." Every bug in LoMBot's `drive_planner`
(16 closed issues, see Epic #59 §5) was a *scan-classification* error that
produced either a nag (false positive) or a silent miss (false negative).
So the scan is the brain: get the bucketing right, bake in the scars, and
make the output auditable rather than silently dropping events.

This module is the deterministic core (per `coding-policy: script-
delegation` — classification is a pure function of known inputs). It takes
the events JSON from the wide-window calendar fetch plus the current time,
the skip-state, and the home address, and returns one `MeetingClass` per
event. It does NOT route, fetch, or write — routing needs live traffic
(`maps_client`) and happens downstream; the scan only flags *which* legs
need routing and what their deadlines are.

Buckets (Epic #59 §3, §5):
    needs_decision  An in-person meeting with no planner block and no skip
                    — propose drive/skip (outbound from home + return home).
    bridge          Tight gap to a DIFFERENT venue — a venue→venue leg, not
                    a home round-trip. `gap_seconds` is exposed so the
                    router can warn when drive_time > gap (lombot #14/#7).
    back_to_back    Tight gap to the SAME venue — you stay put, no transit
                    leg between the two (lombot #14/#7).
    has_block       A planner marker block already references this meeting.
                    "Handled" = ANY marker exists, not both directions
                    (lombot #50 — requiring both caused 6 duplicate blocks).
    skipped         The user said "skip" and the skip has not expired
                    (lombot #49 — never re-ask a live skip).
    past            start ≤ now (small tolerance) — never plan into the
                    past (lombot #28).
    filtered        Not a routable ground meeting: all-day, virtual
                    location, the planner's own block, or an unparseable /
                    missing time. Returned (not dropped) so the sweep can
                    audit and clean up — the meta-lesson is "no silent miss".

Lessons baked in (Epic #59 §5):
    #50  has_block = ANY marker; idempotent — caller checks before insert.
    #49  skips persist with expiry; virtual filtered at scan, never asked.
    #28  past guard everywhere — filter start ≤ now, no past legs.
    #14/#7  neighbour-aware: same venue+tight = back_to_back (no leg);
            different venue+tight = bridge; expose gap for the drive>gap warn.
    #37  normalize whitespace in `location` before it reaches routing.
    #2/#40  return + bridge are first-class; mode inherits outbound (the
            car is at the venue) — recorded on the leg for the router.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from scan import scan, MeetingClass, TransitLeg, ScanError

    results = scan(
        events,                      # list of Google Calendar event dicts
        now=datetime.now(tz=...),    # tz-aware "current" time
        home_address="1040 Pine Creek Dr, Arrington, TN 37014",
        skip_state={"evt_3": "2026-07-01T00:00:00+00:00"},  # id -> expiry
    )
    for r in results:
        if r.bucket == "needs_decision":
            ...  # ask drive/skip, route r.legs
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# A meeting whose start is at or before `now` is in the past and is never
# planned (lombot #28). A small grace window keeps a meeting that started a
# minute ago — while the user is en route — out of the `past` bucket.
PAST_TOLERANCE = timedelta(minutes=5)

# Two consecutive meetings closer than this are "tight": not worth (or not
# possible) returning home between them. Same venue + tight = stay put
# (back_to_back); different venue + tight = drive straight across (bridge).
# A black-box constant (per `coding-policy: script-as-black-box`); callers
# override via `tight_gap_seconds=` and tests pin the boundary.
DEFAULT_TIGHT_GAP_SECONDS = 90 * 60

# The marker drive-planner stamps into the description of every block it
# creates, so it recognizes its own work (idempotency, lombot #50) and can
# attribute a block to the meeting it serves. Example:
#     [drive-planner:meeting=evt_42:dir=outbound]
_MARKER_RE = re.compile(r"\[drive-planner:meeting=(?P<id>[^:\]]+):dir=(?P<dir>[^:\]]+)\]")

# Substrings that mark a `location` as a virtual meeting, not a place to
# drive to (lombot #49 — filter at scan, never ask). A URL anywhere in the
# location is treated as virtual: real venues are addresses, not links.
_VIRTUAL_MARKERS = (
    "zoom.us",
    "meet.google.com",
    "teams.microsoft.com",
    "teams.live.com",
    "webex.com",
    "http://",
    "https://",
    "online",
    "virtual",
    "phone call",
    "google meet",
)


class ScanError(ValueError):
    """Raised on a malformed scan input the caller must fix.

    A ValueError subclass — the fix is "pass a well-formed input" (a
    tz-aware `now`, a real list of events), not "retry".
    """


@dataclass(frozen=True)
class TransitLeg:
    """One drive leg the scan says should exist for a meeting.

    The scan computes the leg and its deadline; it does NOT compute drive
    time (that needs live traffic, downstream). `gap_seconds` is populated
    for bridge legs so the router can warn when drive_time > gap_seconds
    (lombot #14/#7).

    Fields:
        direction: "outbound" (home/prior venue → meeting), "return"
            (meeting → home), or "bridge" (prior venue → meeting, tight gap)
        origin: where the leg starts (home address or the prior venue)
        destination: where the leg ends (the meeting venue or home)
        arrive_by: hard arrival deadline (meeting start) for legs that must
            land before the meeting; None for a return leg
        depart_after: earliest departure (meeting end) for a return leg;
            None for arrival-anchored legs
        gap_seconds: for a bridge, seconds between the prior meeting's end
            and this meeting's start — the budget the drive must fit inside;
            None for non-bridge legs
    """

    direction: str
    origin: str
    destination: str
    arrive_by: datetime | None = None
    depart_after: datetime | None = None
    gap_seconds: int | None = None


@dataclass(frozen=True)
class MeetingClass:
    """One event's classification and the transit work it implies.

    Fields:
        meeting_id: the source event id
        summary: the event summary (for the user-facing drive/skip prompt)
        bucket: one of needs_decision / bridge / back_to_back / has_block /
            skipped / past / filtered
        reason: a short, audit-friendly explanation of the bucket choice
        location: whitespace-normalized venue (lombot #37); None when the
            event has no usable location
        start: parsed event start (tz-aware); None when unparseable / all-day
        end: parsed event end (tz-aware); None when unparseable / all-day
        legs: drive legs to create for this meeting (empty unless the bucket
            is needs_decision / bridge / back_to_back)
        present_directions: for has_block, the marker directions already on
            the calendar (e.g. ["outbound"]) so the sweep can audit a missing
            return without flipping the meeting back to needs_decision
            (lombot #48/#50)
    """

    meeting_id: str
    summary: str
    bucket: str
    reason: str
    location: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    legs: tuple[TransitLeg, ...] = ()
    present_directions: tuple[str, ...] = ()


@dataclass
class _Event:
    """Internal parsed view of a raw Google Calendar event."""

    raw_id: str
    summary: str
    location: str | None
    start: datetime | None
    end: datetime | None
    all_day: bool
    marker: tuple[str, str] | None  # (served_meeting_id, direction) if a block


def _normalize_location(location: str | None) -> str | None:
    """Collapse all whitespace runs to single spaces and strip (lombot #37).

    A multi-line `location` (venue name `\\n` street address) crashed
    LoMBot's geocoder; normalizing here means every downstream router sees
    one clean line. Returns None for an empty / whitespace-only location.
    """
    if not location:
        return None
    collapsed = re.sub(r"\s+", " ", location).strip()
    return collapsed or None


def _is_virtual(location: str | None) -> bool:
    """True when the (already-normalized) location is a virtual meeting."""
    if not location:
        return False
    lowered = location.lower()
    return any(marker in lowered for marker in _VIRTUAL_MARKERS)


def _parse_dt(block: dict | None) -> tuple[datetime | None, bool]:
    """Parse a Google Calendar start/end block.

    Returns (datetime, all_day). A timed event carries `dateTime`
    (ISO-8601 with offset); an all-day event carries `date` (no time) and
    is never a drive target. Returns (None, False) for a missing / malformed
    / timezone-naive block so the caller can filter it as unparseable — a
    naive datetime can't be compared to the tz-aware `now` without raising.
    """
    if not isinstance(block, dict):
        return None, False
    if "date" in block and "dateTime" not in block:
        return None, True
    return _parse_iso(block.get("dateTime")), False


def _parse_iso(raw: object) -> datetime | None:
    """Parse an ISO-8601 / RFC3339 string into a tz-aware datetime, or None.

    Normalizes a trailing `Z` to `+00:00` (RFC3339 UTC, which some sources
    emit) and rejects a timezone-naive result: a naive datetime compared to
    the tz-aware `now` raises TypeError, so it is "unparseable" for our
    purposes, not a usable time.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _parse_event(raw: dict) -> _Event:
    """Adapt a raw Google Calendar event dict into an `_Event`."""
    raw_id = str(raw.get("id") or "")
    summary = str(raw.get("summary") or "")
    location = _normalize_location(raw.get("location"))
    start, start_all_day = _parse_dt(raw.get("start"))
    end, end_all_day = _parse_dt(raw.get("end"))

    marker_match = _MARKER_RE.search(raw.get("description") or "")
    marker = (marker_match["id"], marker_match["dir"]) if marker_match else None

    return _Event(
        raw_id=raw_id,
        summary=summary,
        location=location,
        start=start,
        end=end,
        all_day=start_all_day or end_all_day,
        marker=marker,
    )


def _skip_active(skip_state: dict[str, str], meeting_id: str, now: datetime) -> bool:
    """True when a non-expired skip exists for this meeting (lombot #49).

    A malformed or expired expiry is treated as "no skip" — the meeting
    re-enters needs_decision rather than being silently suppressed forever.
    """
    expiry = _parse_iso(skip_state.get(meeting_id))
    if expiry is None:
        return False
    return expiry > now


def _is_past(event: _Event, now: datetime) -> bool:
    """True when the event has already started (lombot #28), with a grace window."""
    return event.start is not None and event.start <= now - PAST_TOLERANCE


def _is_routable_candidate(event: _Event, now: datetime) -> bool:
    """True when the event can act as a real-meeting neighbour for §5 #14/#7.

    A routable candidate is a future, timed, in-person, non-block meeting
    with a usable location. Excluding past meetings here is the fix for the
    cross of lombot #28 and #14/#7: a stale same-venue meeting must not turn
    a future meeting into back_to_back and strip its outbound-from-home leg.
    `end` is required too, so a half-parsed event never skews a gap.
    """
    return (
        event.marker is None
        and not event.all_day
        and event.start is not None
        and event.end is not None
        and event.location is not None
        and not _is_virtual(event.location)
        and not _is_past(event, now)
    )


def scan(
    events: list[dict],
    *,
    now: datetime,
    home_address: str,
    skip_state: dict[str, str] | None = None,
    tight_gap_seconds: int = DEFAULT_TIGHT_GAP_SECONDS,
) -> list[MeetingClass]:
    """Classify every event into a drive-planner bucket.

    Pure and deterministic: same inputs → same output, no I/O. Returns one
    `MeetingClass` per input event (nothing is silently dropped — filtered
    events come back with bucket="filtered" and a reason so the sweep can
    audit and clean up).

    Args:
        events: raw Google Calendar event dicts (id, summary, location,
            start, end, description) from the wide-window fetch.
        now: tz-aware current time. A naive datetime raises ScanError —
            comparing it to tz-aware event times would be wrong, not just
            an exception.
        home_address: the drive origin/return for home-anchored legs.
        skip_state: meeting_id → ISO-8601 expiry; an unexpired entry buckets
            the meeting as skipped. Defaults to no skips.
        tight_gap_seconds: gap at or below which two consecutive meetings
            are "tight" (bridge / back_to_back). Defaults to
            DEFAULT_TIGHT_GAP_SECONDS.

    Returns:
        list[MeetingClass] in the input order.

    Raises:
        ScanError: if `now` is naive, `home_address` is empty, or `events`
            is not a list.
    """
    if now.tzinfo is None:
        raise ScanError(
            "scan: `now` is timezone-naive — pass a tz-aware datetime "
            "(datetime.now(tz=ZoneInfo(...))) so it compares to event times"
        )
    if not home_address:
        raise ScanError(
            "scan: `home_address` is empty — read it from the canonical "
            "user_profile.md Addresses block (current_home)"
        )
    if not isinstance(events, list):
        raise ScanError(f"scan: `events` must be a list, got {type(events).__name__}")

    skip_state = skip_state or {}
    parsed = [_parse_event(raw) for raw in events]

    # Pass 1: every meeting referenced by ANY planner marker is "handled"
    # (lombot #50 — ANY marker, not both directions). Record which
    # directions are already present so the sweep can audit completeness.
    handled_directions: dict[str, list[str]] = {}
    for event in parsed:
        if event.marker is not None:
            served_id, direction = event.marker
            handled_directions.setdefault(served_id, []).append(direction)

    # Pass 2: order the genuine ground meetings by start so each can read its
    # neighbours (lombot #14/#7). Only routable candidates are linked — and
    # crucially that EXCLUDES past meetings (lombot #28): an already-past
    # same-venue meeting must never make a future meeting back_to_back and
    # strip its outbound-from-home leg. A non-candidate still gets classified,
    # it just can't act as a neighbour.
    candidates = [event for event in parsed if _is_routable_candidate(event, now)]
    candidates.sort(key=lambda e: e.start)  # type: ignore[arg-type,return-value]
    order = {id(event): index for index, event in enumerate(candidates)}

    results: list[MeetingClass] = []
    for event in parsed:
        results.append(
            _classify(
                event,
                now=now,
                home_address=home_address,
                skip_state=skip_state,
                handled_directions=handled_directions,
                candidates=candidates,
                order=order,
                tight_gap_seconds=tight_gap_seconds,
            )
        )
    return results


def _make_class(
    event: _Event,
    bucket: str,
    reason: str,
    *,
    legs: tuple[TransitLeg, ...] = (),
    present_directions: tuple[str, ...] = (),
) -> MeetingClass:
    """Build a MeetingClass from an event, carrying its identity fields over."""
    return MeetingClass(
        meeting_id=event.raw_id,
        summary=event.summary,
        bucket=bucket,
        reason=reason,
        location=event.location,
        start=event.start,
        end=event.end,
        legs=legs,
        present_directions=present_directions,
    )


def _classify(
    event: _Event,
    *,
    now: datetime,
    home_address: str,
    skip_state: dict[str, str],
    handled_directions: dict[str, list[str]],
    candidates: list[_Event],
    order: dict[int, int],
    tight_gap_seconds: int,
) -> MeetingClass:
    """Assign one event to a bucket. Precedence matters — see inline order."""
    # 1. The planner's own blocks are never meetings to plan (filtered), but
    #    they are how Pass 1 learned what is handled.
    if event.marker is not None:
        return _make_class(event, "filtered", "planner block")

    # 2. All-day events have no drive deadline.
    if event.all_day:
        return _make_class(event, "filtered", "all-day event")

    # 3. Unparseable / missing time — can't anchor a leg; surface, don't drop.
    if event.start is None or event.end is None:
        return _make_class(event, "filtered", "missing or unparseable time")

    # 4. Virtual / no location — never ask (lombot #49).
    if event.location is None:
        return _make_class(event, "filtered", "no location")
    if _is_virtual(event.location):
        return _make_class(event, "filtered", "virtual location")

    # 5. Past guard (lombot #28) — never plan into the past.
    if _is_past(event, now):
        return _make_class(event, "past", "meeting already started")

    # 6. Already handled — ANY marker counts (lombot #50). Wins over
    #    needs_decision so the planner never re-asks or double-books.
    if event.raw_id in handled_directions:
        present = tuple(dict.fromkeys(handled_directions[event.raw_id]))
        return _make_class(
            event,
            "has_block",
            f"planner block(s) present: {', '.join(present)}",
            present_directions=present,
        )

    # 7. Live skip (lombot #49) — the user said no; don't ask again.
    if _skip_active(skip_state, event.raw_id, now):
        return _make_class(event, "skipped", "user-skipped, not expired")

    # 8. A routable meeting — read neighbours and emit legs.
    return _classify_transit(
        event,
        home_address=home_address,
        candidates=candidates,
        order=order,
        tight_gap_seconds=tight_gap_seconds,
    )


def _classify_transit(
    event: _Event,
    *,
    home_address: str,
    candidates: list[_Event],
    order: dict[int, int],
    tight_gap_seconds: int,
) -> MeetingClass:
    """Neighbour-aware leg computation (lombot #14/#7, #2/#40).

    Outbound: skipped when the previous meeting is the SAME venue with a
    tight gap (back_to_back — you're already there); a bridge when the
    previous meeting is a DIFFERENT venue with a tight gap; otherwise a
    home→venue leg. Return is the mirror on the next meeting. Anchoring
    outbound on the first of a same-venue run and return on the last falls
    out of this naturally.
    """
    index = order[id(event)]
    prev_event = candidates[index - 1] if index > 0 else None
    next_event = candidates[index + 1] if index + 1 < len(candidates) else None

    legs: list[TransitLeg] = []
    is_bridge = False
    is_back_to_back = False

    # --- inbound side: how do we get TO this meeting? ---
    prev_gap = _gap_seconds(prev_event, event)
    if prev_event is not None and prev_gap is not None and prev_gap <= tight_gap_seconds:
        if _same_venue(prev_event.location, event.location):
            # Same venue, tight gap: you never left — no inbound leg.
            is_back_to_back = True
        else:
            # Different venue, tight gap: drive straight across, not via home.
            is_bridge = True
            legs.append(
                TransitLeg(
                    direction="bridge",
                    origin=prev_event.location or home_address,
                    destination=event.location or home_address,
                    arrive_by=event.start,
                    gap_seconds=prev_gap,
                )
            )
    else:
        legs.append(
            TransitLeg(
                direction="outbound",
                origin=home_address,
                destination=event.location or home_address,
                arrive_by=event.start,
            )
        )

    # --- return side: how do we get home AFTER this meeting? ---
    # A tight gap to ANY next meeting cancels the return-home leg: same
    # venue means you stay put, different venue means the next meeting owns
    # the bridge leg in (lombot #14/#7) — either way you don't drive home.
    next_gap = _gap_seconds(event, next_event)
    skip_return = next_event is not None and next_gap is not None and next_gap <= tight_gap_seconds
    if not skip_return:
        legs.append(
            TransitLeg(
                direction="return",
                origin=event.location or home_address,
                destination=home_address,
                depart_after=event.end,
            )
        )

    if is_bridge:
        bucket, reason = "bridge", "tight gap to a different venue"
    elif is_back_to_back:
        bucket, reason = "back_to_back", "tight gap to the same venue"
    else:
        bucket, reason = "needs_decision", "standalone in-person meeting"

    return _make_class(event, bucket, reason, legs=tuple(legs))


def _gap_seconds(earlier: _Event | None, later: _Event | None) -> int | None:
    """Seconds between `earlier.end` and `later.start`; None if either is missing."""
    if earlier is None or later is None or earlier.end is None or later.start is None:
        return None
    return int((later.start - earlier.end).total_seconds())


def _same_venue(a: str | None, b: str | None) -> bool:
    """Case-insensitive equality of two normalized venue strings.

    Both are already whitespace-normalized (lombot #37); equality on the
    cleaned string is enough to tell "same place" from "different place"
    without geocoding (which is downstream and live).
    """
    if a is None or b is None:
        return False
    return a.casefold() == b.casefold()


def actionable(results: list[MeetingClass]) -> list[MeetingClass]:
    """Filter to the buckets that need the planner to do something now.

    needs_decision / bridge / back_to_back are the buckets that produce a
    drive/skip prompt and new blocks. has_block / skipped / past / filtered
    are terminal for this sweep (the sweep audits them separately).
    """
    actionable_buckets = {"needs_decision", "bridge", "back_to_back"}
    return [r for r in results if r.bucket in actionable_buckets]
