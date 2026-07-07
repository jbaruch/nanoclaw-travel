"""Encode/decode a drive-planner drive block — the calendar IS the state.

drive-planner does not keep a local store of the blocks it created; the created
calendar event itself carries everything the recheck poll needs to re-evaluate
it (Epic #59 §4 — calendar event as state, read back by a direct API fetch).
This module is that codec: it builds the `GOOGLECALENDAR_CREATE_EVENT`
arguments for a block on the way out, and parses a fetched event back into a
typed `BlockState` on the way in.

State lives entirely in the event **description** — the live Composio v3
calendar toolkit exposes NO `extendedProperties` on any create/patch/update
action (verified against the NAS during Phase 1), so the earlier
extendedProperties.private design is impossible there. The description carries:

  * the human line (`Drive: <summary>`),
  * the self-marker `[drive-planner:meeting=<id>:dir=<dir>]` — `scan.py` reads
    this token to recognize the planner's own work (idempotency, lombot #50),
    so the marker MUST match `scan._MARKER_RE` (a test pins it), and
  * a compact `<!--dp:{...}-->` JSON comment with the machine state the recheck
    poll reads back: schema version, baseline drive seconds, arrive-by, the
    routed origin/destination (to re-route the same leg), and the
    alert-suppression record. Parsed defensively — a malformed comment yields
    `None`, never raises.

The write contract (live v3): flat `start_datetime` + `event_duration_*` (no
nested `start.dateTime`, no `end_datetime`); `location` carries the venue;
`transparency` is "transparent" (Free) unless `busy` (Epic #59 §5). Suppression
updates re-write the description via `GOOGLECALENDAR_PATCH_EVENT` (which does
support `description`), so `build_description` is the single source of the
description format for both create and the suppression patch.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from block_props import build_block_args, build_description, parse_block, BlockState

    args = build_block_args(
        calendar_id="primary", meeting_id="evt_42", direction="outbound",
        summary="Drive: Customer sync", leg_start=depart_dt,
        arrive_by=meeting_start, baseline_seconds=1500,
        origin="12 Example St, Sampleton, TN 37000",
        destination="100 Broadway, Nashville, TN",
    )
    # ... create_event(args) ...
    state = parse_block(fetched_event)   # -> BlockState | None
    if state and state.due_for_recheck(now):
        ...  # re-route state.origin -> state.destination, gate via recheck.py
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Marker token stamped into the block description so the planner (and `scan.py`)
# recognize their own work. MUST stay byte-compatible with `scan._MARKER_RE`;
# `test_block_props.py` asserts the two agree. The parse regex is defined here
# too so this module stays self-contained (no import of scan's internals).
_MARKER_TEMPLATE = "[drive-planner:meeting={meeting_id}:dir={direction}]"
_MARKER_RE = re.compile(r"\[drive-planner:meeting=(?P<id>[^:\]]+):dir=(?P<dir>[^:\]]+)\]")

# Schema version of the calendar-as-state block record (per `coding-policy:
# stateful-artifacts` — every persisted record carries a version so migrations
# are auditable). Bump on any shape change to the description state JSON and add
# the owner-side upgrade in `parse_block`.
#   v1 — the original `extendedProperties.private` string-map shape (defunct:
#        the live v3 toolkit has no writable extendedProperties, so no v1 record
#        was ever successfully written; the new parser can't read that shape
#        regardless, since it carries no `<!--dp:-->` description comment).
#   v2 — the description `<!--dp:{...}-->` JSON shape (current).
BLOCK_SCHEMA_VERSION = 2

# The machine-state JSON rides in an HTML comment so it stays out of the way in
# calendar UIs while remaining round-trippable. Short keys keep the description
# compact; addresses (commas, etc.) survive because the payload is JSON.
_STATE_RE = re.compile(r"<!--dp:(?P<json>\{.*?\})-->", re.DOTALL)
_STATE_KEY_VERSION = "v"
_STATE_KEY_BASELINE = "b"
_STATE_KEY_ARRIVE_BY = "a"
_STATE_KEY_ORIGIN = "o"
_STATE_KEY_DESTINATION = "d"
_STATE_KEY_ALERTED = "al"

ALERT_GROWTH = "growth"
ALERT_LEAVE_NOW = "leave_now"
_ALERT_VALUES = (ALERT_GROWTH, ALERT_LEAVE_NOW)

# Arrival slack folded into leave_by, mirroring recheck.py's default so the
# poll's window math and the gate agree on when "leave by" is.
DEFAULT_ARRIVAL_BUFFER_SECONDS = 5 * 60

# How far ahead of a block's leave-by the recheck poll starts evaluating it, and
# how long after departure it keeps evaluating before giving up. With a ~15-min
# poll cadence over this 45-min horizon, a block is naturally re-evaluated at
# roughly T-45/T-30/T-15 (Epic #59 §3). Black-box constants (per `coding-policy:
# script-as-black-box`); the poll overrides via `horizon_seconds=`.
DEFAULT_RECHECK_HORIZON_SECONDS = 45 * 60
DEFAULT_DEPARTED_GRACE_SECONDS = 15 * 60


def build_marker(meeting_id: str, direction: str) -> str:
    """The self-marker token for a block serving `meeting_id` in `direction`."""
    return _MARKER_TEMPLATE.format(meeting_id=meeting_id, direction=direction)


def parse_marker(text: object) -> tuple[str, str] | None:
    """Extract `(meeting_id, direction)` from a description marker, or None."""
    if not isinstance(text, str):
        return None
    match = _MARKER_RE.search(text)
    return (match["id"], match["dir"]) if match else None


def serialize_alerted(alerted: frozenset | set) -> str:
    """Serialize an alert set to the stable comma-joined record."""
    return ",".join(value for value in _ALERT_VALUES if value in alerted)


def parse_alerted(raw: object) -> frozenset:
    """Parse the comma-joined alert-suppression record into a set.

    Unknown tokens are dropped; a non-string yields the empty set. Tolerant by
    design — a corrupt record must not crash the poll, at worst it re-sends an
    alert (annoying, not unsafe).
    """
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(token.strip() for token in raw.split(",") if token.strip() in _ALERT_VALUES)


def build_description(
    *,
    summary: str,
    meeting_id: str,
    direction: str,
    baseline_seconds: int,
    arrive_by: datetime,
    origin: str,
    destination: str,
    alerted: frozenset | set = frozenset(),
) -> str:
    """The full block description: human line + scan marker + state JSON comment.

    The single source of the description format — `build_block_args` uses it for
    create, and the recheck poll re-runs it (with an updated `alerted`) to PATCH
    the suppression record back onto the event.
    """
    state = {
        _STATE_KEY_VERSION: BLOCK_SCHEMA_VERSION,
        _STATE_KEY_BASELINE: baseline_seconds,
        _STATE_KEY_ARRIVE_BY: arrive_by.isoformat(),
        _STATE_KEY_ORIGIN: origin,
        _STATE_KEY_DESTINATION: destination,
        _STATE_KEY_ALERTED: serialize_alerted(alerted),
    }
    marker = build_marker(meeting_id, direction)
    blob = json.dumps(state, separators=(",", ":"))
    return f"{summary}\n{marker}\n<!--dp:{blob}-->"


def _duration_minutes(leg_start: datetime, leg_end: datetime) -> int:
    """Whole-minute duration for the create call (always at least 1 minute)."""
    minutes = round((leg_end - leg_start).total_seconds() / 60)
    return max(minutes, 1)


def _wall_clock_in(dt: datetime, tz_name: str | None) -> datetime:
    """Express `dt` in the CREATE's `timezone` arg so wall-clock and tz agree.

    The Composio `GOOGLECALENDAR_CREATE_EVENT` adapter ignores the offset in
    `start_datetime` and re-reads its wall-clock in the `timezone` arg, so a
    leg computed in the home offset but created with the venue tz lands
    shifted by the home↔venue delta (#131 — 6h early on a UK trip).

    `dt` is returned as-is in two cases: `tz_name` absent (the caller then
    omits the CREATE's `timezone` arg entirely, and `dt`'s own offset is
    the correct instant), or `tz_name` not a resolvable zone key (real IANA
    names and `_extract_timezone`'s `Etc/GMT±N` fallback both resolve —
    this is defensive). In the unresolvable case the caller still passes
    `tz_name` through as the `timezone` arg, preserving the pre-#131
    behavior for that path: no conversion this helper could do would be
    more correct than the wall-clock `dt` already carries.
    """
    if not tz_name:
        return dt
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt
    return dt.astimezone(zone)


def build_block_args(
    *,
    calendar_id: str,
    meeting_id: str,
    direction: str,
    summary: str,
    leg_start: datetime,
    arrive_by: datetime,
    baseline_seconds: int,
    origin: str,
    destination: str,
    leg_end: datetime | None = None,
    busy: bool = False,
    timezone: str | None = None,
) -> dict:
    """Build the `GOOGLECALENDAR_CREATE_EVENT` arguments for a drive block.

    The live v3 contract: flat `start_datetime` + `event_duration_hour` /
    `event_duration_minutes` (no nested start/end, no extendedProperties);
    `location` is the destination; the machine state rides in the description
    (see `build_description`). The block is Free (`transparency: "transparent"`)
    unless `busy`.

    Args:
        calendar_id: the calendar to create the block on.
        meeting_id: the served meeting's event id.
        direction: "outbound" / "return" / "bridge".
        summary: the block's human title.
        leg_start: when the block starts (departure time).
        arrive_by: the hard arrival deadline (meeting start). For a return leg
            with no arrival deadline, pass the leg end here too — the recheck
            poll skips return legs by `direction`, so a return's arrive_by is
            recorded, never used as a deadline.
        baseline_seconds: routed drive seconds captured at creation.
        origin / destination: the routed leg endpoints (the poll re-routes
            exactly this pair).
        leg_end: block end; defaults to `arrive_by`.
        busy: create the block Busy instead of Free.
        timezone: the meeting's IANA timezone (e.g. "America/Chicago"). Emitted
            as the live CREATE's `timezone` arg so the block lands at the right
            instant — without it Composio reads the wall-clock as UTC and the
            block lands hours off (#83). Omitted from the args when None.

    Returns:
        a dict of create-event arguments (calendar_id, summary, description,
        location, start_datetime, event_duration_hour/minutes, transparency,
        and `timezone` when provided).

    Raises:
        ValueError: on a naive datetime, an empty endpoint, a negative or
            non-int baseline, or an unknown direction.
    """
    for label, value in (("leg_start", leg_start), ("arrive_by", arrive_by)):
        if value.tzinfo is None:
            raise ValueError(f"build_block_args: `{label}` must be timezone-aware")
    if leg_end is not None and leg_end.tzinfo is None:
        raise ValueError("build_block_args: `leg_end` must be timezone-aware")
    if not meeting_id:
        raise ValueError("build_block_args: `meeting_id` must be non-empty")
    if direction not in ("outbound", "return", "bridge"):
        raise ValueError(
            f"build_block_args: `direction` must be outbound/return/bridge (got {direction!r})"
        )
    if not isinstance(baseline_seconds, int) or isinstance(baseline_seconds, bool):
        raise ValueError("build_block_args: `baseline_seconds` must be an int")
    if baseline_seconds < 0:
        raise ValueError("build_block_args: `baseline_seconds` must be non-negative")
    if not origin or not destination:
        raise ValueError("build_block_args: `origin` and `destination` must be non-empty")

    end = leg_end if leg_end is not None else arrive_by
    total_minutes = _duration_minutes(leg_start, end)
    description = build_description(
        summary=summary,
        meeting_id=meeting_id,
        direction=direction,
        baseline_seconds=baseline_seconds,
        arrive_by=arrive_by,
        origin=origin,
        destination=destination,
    )
    args = {
        "calendar_id": calendar_id,
        "summary": summary,
        "description": description,
        "location": destination,
        "start_datetime": _wall_clock_in(leg_start, timezone).isoformat(),
        "event_duration_hour": total_minutes // 60,
        "event_duration_minutes": total_minutes % 60,
        "transparency": "opaque" if busy else "transparent",
    }
    # The live CREATE needs an explicit IANA `timezone`, or it reads the
    # wall-clock as UTC and the block lands hours off (#83). When the meeting
    # carries no timeZone, omit it rather than guess — the caller anchors the
    # block to the same instant either way. The wall-clock above is expressed
    # in this same zone (#131) — the adapter drops the offset and re-reads the
    # wall-clock in `timezone`, so the two must agree.
    if timezone:
        args["timezone"] = timezone
    return args


@dataclass(frozen=True)
class BlockState:
    """A drive-planner block parsed back off a fetched calendar event.

    Carries exactly what the recheck poll needs to re-evaluate one block without
    any local store: the leg endpoints to re-route, the baseline to compare
    against, the arrive-by deadline, which alerts already fired, and the event's
    summary (so the suppression patch can rebuild the full description).

    Fields:
        event_id: the block event's own calendar id (for the suppression patch).
        calendar_id: the calendar the block lives on (None when the fetch did
            not attribute one; the poll falls back to its configured calendar).
        meeting_id: the served meeting's id.
        direction: "outbound" / "return" / "bridge".
        summary: the block's human title (to rebuild the description on patch).
        baseline_seconds: routed drive seconds captured at creation.
        arrive_by: the hard arrival deadline (tz-aware).
        origin / destination: the routed leg endpoints.
        alerted: the set of alerts already sent ({"growth", "leave_now"}).
        buffer_seconds: arrival slack folded into leave_by.
    """

    event_id: str
    meeting_id: str
    direction: str
    summary: str
    baseline_seconds: int
    arrive_by: datetime
    origin: str
    destination: str
    calendar_id: str | None = None
    alerted: frozenset = field(default_factory=frozenset)
    buffer_seconds: int = DEFAULT_ARRIVAL_BUFFER_SECONDS

    @property
    def baseline_leave_by(self) -> datetime:
        """When you must leave at the baseline drive time (arrive_by − drive − buffer)."""
        return self.arrive_by - timedelta(seconds=self.baseline_seconds + self.buffer_seconds)

    def due_for_recheck(
        self,
        now: datetime,
        *,
        horizon_seconds: int = DEFAULT_RECHECK_HORIZON_SECONDS,
        departed_grace_seconds: int = DEFAULT_DEPARTED_GRACE_SECONDS,
    ) -> bool:
        """True when `now` is inside this block's recheck window.

        The window opens `horizon_seconds` before the baseline leave-by and
        closes `departed_grace_seconds` after it.
        """
        leave_by = self.baseline_leave_by
        opens = leave_by - timedelta(seconds=horizon_seconds)
        closes = leave_by + timedelta(seconds=departed_grace_seconds)
        return opens <= now <= closes

    def already_alerted(self, kind: str) -> bool:
        """True when an alert of `kind` ("growth" / "leave_now") already fired."""
        return kind in self.alerted

    def description_with_alerts(self, alerted: frozenset | set) -> str:
        """Rebuild the full block description with an updated alert record."""
        return build_description(
            summary=self.summary,
            meeting_id=self.meeting_id,
            direction=self.direction,
            baseline_seconds=self.baseline_seconds,
            arrive_by=self.arrive_by,
            origin=self.origin,
            destination=self.destination,
            alerted=alerted,
        )


def _parse_iso(raw: object) -> datetime | None:
    """Parse an ISO-8601 / RFC3339 string into a tz-aware datetime, or None."""
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


def next_alerts(
    alerted: frozenset | set, *, grew: bool, leave_now: bool
) -> tuple[tuple[str, ...], frozenset]:
    """Decide which alerts fire now and the suppression record after.

    The recheck gate reports `grew` (traffic grew past the threshold) and
    `leave_now` (the recomputed leave-by has arrived). Each fires AT MOST ONCE
    per block: re-pinging a still-grown drive every poll is the trust-eroding
    nag (Epic #59 §5 #49 in spirit). Returns `(kinds_to_fire, new_alerted)`;
    `kinds_to_fire` is empty when both were already alerted (the poll then stays
    silent for this block and patches nothing).
    """
    fire: list[str] = []
    if grew and ALERT_GROWTH not in alerted:
        fire.append(ALERT_GROWTH)
    if leave_now and ALERT_LEAVE_NOW not in alerted:
        fire.append(ALERT_LEAVE_NOW)
    new_alerted = frozenset(alerted) | frozenset(fire)
    return tuple(fire), new_alerted


def _decode_state(description: object) -> dict | None:
    """Pull the `<!--dp:{...}-->` state JSON out of a description, defensively."""
    if not isinstance(description, str):
        return None
    match = _STATE_RE.search(description)
    if match is None:
        return None
    try:
        decoded = json.loads(match["json"])
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def parse_block(event: object) -> BlockState | None:
    """Parse a fetched calendar event into a `BlockState`, or None.

    Recognition is by the description's `<!--dp:{...}-->` state JSON plus the
    `[drive-planner:meeting=...:dir=...]` marker. Returns None when the event
    carries no drive-planner state, the marker is absent/malformed, or a
    required field is missing — the recheck poll treats None as "not a block I
    recheck" and moves on.

    Schema version (per `coding-policy: stateful-artifacts`): a record stamped
    NEWER than this plugin supports reads as None — no-usable-prior-state, the
    safe non-disruptive fallback. A missing version is treated as v1.
    """
    if not isinstance(event, dict):
        return None
    description = event.get("description")
    state = _decode_state(description)
    if state is None:
        return None

    # A missing version is treated as v1 (back-compat). A present version that
    # is newer than this plugin supports — OR not a plain int at all (a corrupt
    # or future-shaped record, e.g. "2") — reads as no-usable-prior-state so the
    # poll skips it, the safe non-disruptive fallback.
    version = state.get(_STATE_KEY_VERSION)
    if version is not None:
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version > BLOCK_SCHEMA_VERSION
        ):
            return None

    marker = _MARKER_RE.search(description) if isinstance(description, str) else None
    if marker is None:
        return None
    meeting_id = marker["id"]
    direction = marker["dir"]
    if direction not in ("outbound", "return", "bridge"):
        return None

    baseline = state.get(_STATE_KEY_BASELINE)
    arrive_by = _parse_iso(state.get(_STATE_KEY_ARRIVE_BY))
    origin = state.get(_STATE_KEY_ORIGIN)
    destination = state.get(_STATE_KEY_DESTINATION)
    if not isinstance(baseline, int) or isinstance(baseline, bool) or baseline < 0:
        return None
    if arrive_by is None:
        return None
    if not isinstance(origin, str) or not origin:
        return None
    if not isinstance(destination, str) or not destination:
        return None

    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None

    summary = event.get("summary")
    calendar_id = event.get("calendar_id")
    return BlockState(
        event_id=event_id,
        calendar_id=calendar_id if isinstance(calendar_id, str) and calendar_id else None,
        meeting_id=meeting_id,
        direction=direction,
        summary=summary if isinstance(summary, str) else "",
        baseline_seconds=baseline,
        arrive_by=arrive_by,
        origin=origin,
        destination=destination,
        alerted=parse_alerted(state.get(_STATE_KEY_ALERTED)),
    )
