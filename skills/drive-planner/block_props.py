"""Encode/decode a drive-planner drive block — the calendar IS the state.

drive-planner does not keep a local store of the blocks it created; the
created calendar event itself carries everything the recheck poll needs to
re-evaluate it (Epic #59 §4 — calendar event as state, read back by a direct
API fetch, never an agentic read). This module is that codec: it builds the
`GOOGLECALENDAR_CREATE_EVENT` arguments for a block on the way out, and parses
a fetched event back into a typed `BlockState` on the way in.

Two surfaces hold the state, each for a different reader:

  * the event **description** carries the human line plus the self-marker
    token `[drive-planner:meeting=<id>:dir=<dir>]`. `scan.py` reads that
    token to recognize the planner's own work (idempotency, lombot #50), so
    the marker this module emits MUST match `scan._MARKER_RE` — a test pins
    that contract.
  * the event **extendedProperties.private** carries the machine state the
    recheck poll reads back: the served meeting id, leg direction, the
    baseline drive seconds captured at creation, the arrive-by deadline, the
    routed origin/destination (so the poll can re-route the same leg), and an
    alert-suppression record so a block that already pinged is not re-pinged
    every poll. Google Calendar's `extendedProperties.private` is a
    string→string map, so every value is serialized to a string here and
    parsed back defensively (a malformed value never raises — it yields
    `None`, which the poll treats as "not a usable block").

Default-Free transparency (Epic #59 §5 — calls land mid-transit unless the
block is created `--busy`): blocks are created with `transparency:
"transparent"` so the drive time shows as Free.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from block_props import build_block_args, parse_block, BlockState

    args = build_block_args(
        calendar_id="primary",
        meeting_id="evt_42",
        direction="outbound",
        summary="Drive to Customer sync",
        leg_start=depart_dt,        # block start (when you leave)
        arrive_by=meeting_start,    # the hard arrival deadline
        baseline_seconds=1500,      # routed drive time at creation
        origin="12 Example St, Sampleton, TN 37000",
        destination="100 Broadway, Nashville, TN",
    )
    # ... create_event(args) ...

    state = parse_block(fetched_event)   # -> BlockState | None
    if state and state.due_for_recheck(now):
        ...  # re-route state.origin -> state.destination, gate via recheck.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Marker token stamped into the block description so the planner (and
# `scan.py`) recognize their own work. MUST stay byte-compatible with
# `scan._MARKER_RE`; `test_block_props.py` asserts the two agree.
_MARKER_TEMPLATE = "[drive-planner:meeting={meeting_id}:dir={direction}]"

# Schema version of the calendar-as-state block record (per `coding-policy:
# stateful-artifacts` — every persisted record carries a version so migrations
# are auditable). Bump on any shape change to the private-props map and add the
# owner-side upgrade in `parse_block`. v1 is the first and only version.
BLOCK_SCHEMA_VERSION = 1
KEY_SCHEMA_VERSION = "drive_planner_schema_version"

# extendedProperties.private keys. Namespaced `drive_planner_*` so they never
# collide with another tool's private props on the same event.
KEY_MEETING = "drive_planner_meeting"
KEY_DIRECTION = "drive_planner_dir"
KEY_BASELINE = "drive_planner_baseline_seconds"
KEY_ARRIVE_BY = "drive_planner_arrive_by"
KEY_ORIGIN = "drive_planner_origin"
KEY_DESTINATION = "drive_planner_destination"
# Comma-joined record of which alerts already fired for this block, so the
# poll never re-pings the same thing every cycle. Values: "growth" (traffic
# grew past the threshold once) and/or "leave_now" (the leave-by has arrived).
KEY_ALERTED = "drive_planner_alerted"

ALERT_GROWTH = "growth"
ALERT_LEAVE_NOW = "leave_now"
_ALERT_VALUES = (ALERT_GROWTH, ALERT_LEAVE_NOW)

# Arrival slack folded into leave_by, mirroring recheck.py's default so the
# poll's window math and the gate agree on when "leave by" is.
DEFAULT_ARRIVAL_BUFFER_SECONDS = 5 * 60

# How far ahead of a block's leave-by the recheck poll starts evaluating it,
# and how long after departure it keeps evaluating before giving up. With a
# ~15-min poll cadence over this 45-min horizon, a block is naturally
# re-evaluated at roughly T-45/T-30/T-15 (Epic #59 §3) without any per-block
# one-off scheduling. A black-box constant (per `coding-policy:
# script-as-black-box`); the poll overrides via `horizon_seconds=`.
DEFAULT_RECHECK_HORIZON_SECONDS = 45 * 60
# Grace past the leave-by during which a "leave now" is still worth sending
# (you may be running late); beyond it the block is stale and dropped.
DEFAULT_DEPARTED_GRACE_SECONDS = 15 * 60


def build_marker(meeting_id: str, direction: str) -> str:
    """The self-marker token for a block serving `meeting_id` in `direction`."""
    return _MARKER_TEMPLATE.format(meeting_id=meeting_id, direction=direction)


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
) -> dict:
    """Build the `GOOGLECALENDAR_CREATE_EVENT` arguments for a drive block.

    The marker token is appended to the description so `scan.py` recognizes
    the block; the machine state goes into `extendedProperties.private` for
    the recheck poll. The block is Free (`transparency: "transparent"`) unless
    `busy=True`, so a drive block does not auto-decline calls landing
    mid-transit (Epic #59 §5).

    Args:
        calendar_id: the calendar to create the block on.
        meeting_id: the served meeting's event id.
        direction: "outbound" / "return" / "bridge".
        summary: the block's human title.
        leg_start: when the block starts (departure time).
        arrive_by: the hard arrival deadline (meeting start). For a return
            leg with no arrival deadline, pass the leg end here too — the
            recheck poll skips return legs by their `direction` (it only
            rechecks `outbound` / `bridge`), so a return's arrive_by is just
            recorded, never used as a deadline.
        baseline_seconds: routed drive seconds captured at creation.
        origin / destination: the routed leg endpoints (the poll re-routes
            exactly this pair).
        leg_end: block end; defaults to `arrive_by`.
        busy: create the block Busy instead of Free.

    Returns:
        a dict of create-event arguments (calendar_id, summary, start, end,
        description, extendedProperties.private, transparency).

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
    marker = build_marker(meeting_id, direction)
    description = f"{summary}\n{marker}"
    private = {
        KEY_SCHEMA_VERSION: str(BLOCK_SCHEMA_VERSION),
        KEY_MEETING: meeting_id,
        KEY_DIRECTION: direction,
        KEY_BASELINE: str(baseline_seconds),
        KEY_ARRIVE_BY: arrive_by.isoformat(),
        KEY_ORIGIN: origin,
        KEY_DESTINATION: destination,
    }
    return {
        "calendar_id": calendar_id,
        "summary": summary,
        "start": {"dateTime": leg_start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "description": description,
        "extendedProperties": {"private": private},
        "transparency": "opaque" if busy else "transparent",
    }


@dataclass(frozen=True)
class BlockState:
    """A drive-planner block parsed back off a fetched calendar event.

    Carries exactly what the recheck poll needs to re-evaluate one block
    without any local store: the leg endpoints to re-route, the baseline to
    compare against, the arrive-by deadline, and which alerts already fired.

    Fields:
        event_id: the block event's own calendar id (for the alert-suppression
            patch back).
        calendar_id: the calendar the block lives on (None when the fetch did
            not attribute one; the poll falls back to its configured calendar).
        meeting_id: the served meeting's id.
        direction: "outbound" / "return" / "bridge".
        baseline_seconds: routed drive seconds captured at creation.
        arrive_by: the hard arrival deadline (tz-aware).
        origin / destination: the routed leg endpoints.
        alerted: the set of alerts already sent ({"growth", "leave_now"}).
        buffer_seconds: arrival slack folded into leave_by.
    """

    event_id: str
    meeting_id: str
    direction: str
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
        closes `departed_grace_seconds` after it. Outside the window the poll
        skips the block — too early to matter, or so far past departure that a
        ping is noise.
        """
        leave_by = self.baseline_leave_by
        opens = leave_by - timedelta(seconds=horizon_seconds)
        closes = leave_by + timedelta(seconds=departed_grace_seconds)
        return opens <= now <= closes

    def already_alerted(self, kind: str) -> bool:
        """True when an alert of `kind` ("growth" / "leave_now") already fired."""
        return kind in self.alerted


def _parse_iso(raw: object) -> datetime | None:
    """Parse an ISO-8601 / RFC3339 string into a tz-aware datetime, or None.

    Normalizes a trailing `Z` to `+00:00` and rejects a naive result —
    matching scan.py / recheck.py boundary parsing. A non-string or
    unparseable value yields None so a malformed block is dropped, never
    raised on.
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


def _parse_int(raw: object) -> int | None:
    """Parse a base-10 int string, or None on anything malformed."""
    if not isinstance(raw, str):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_alerted(raw: object) -> frozenset:
    """Parse the comma-joined alert-suppression record into a set.

    Unknown tokens are dropped; a non-string yields the empty set. Tolerant by
    design — a corrupt suppression record must not crash the poll, at worst it
    re-sends an alert (annoying, not unsafe).
    """
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(token.strip() for token in raw.split(",") if token.strip() in _ALERT_VALUES)


def serialize_alerted(alerted: frozenset | set) -> str:
    """Serialize an alert set back to the stable comma-joined record."""
    return ",".join(value for value in _ALERT_VALUES if value in alerted)


def next_alerts(
    alerted: frozenset | set, *, grew: bool, leave_now: bool
) -> tuple[tuple[str, ...], frozenset]:
    """Decide which alerts fire now and the suppression record after.

    The recheck gate (`recheck.evaluate_recheck`) reports two conditions —
    `grew` (traffic grew past the threshold) and `leave_now` (the recomputed
    leave-by has arrived). Each fires AT MOST ONCE per block: re-pinging
    "traffic grew" on every poll while it stays grown is the trust-eroding
    nag (Epic #59 §5 #49 in spirit). Given the alerts already sent, return
    `(kinds_to_fire, new_alerted)` — `kinds_to_fire` is empty when both
    conditions were already alerted (the poll then stays silent for this
    block and patches nothing).

    Args:
        alerted: the alerts already sent for this block.
        grew: the gate's `grew_past_threshold`.
        leave_now: the gate's `leave_by_passed`.

    Returns:
        (kinds_to_fire, new_alerted). `new_alerted` is `alerted` unchanged
        when nothing new fires, so the caller can skip the suppression patch.
    """
    fire: list[str] = []
    if grew and ALERT_GROWTH not in alerted:
        fire.append(ALERT_GROWTH)
    if leave_now and ALERT_LEAVE_NOW not in alerted:
        fire.append(ALERT_LEAVE_NOW)
    new_alerted = frozenset(alerted) | frozenset(fire)
    return tuple(fire), new_alerted


def _private_props(event: dict) -> dict:
    """Pull `extendedProperties.private` out of a fetched event, defensively."""
    ext = event.get("extendedProperties")
    if not isinstance(ext, dict):
        return {}
    private = ext.get("private")
    return private if isinstance(private, dict) else {}


def parse_block(event: object) -> BlockState | None:
    """Parse a fetched calendar event into a `BlockState`, or None.

    Recognition is by `extendedProperties.private` (the machine state), not the
    description marker — the marker is for `scan.py`'s idempotency check, this
    parser's contract is the private props. Returns None when the event carries
    no drive-planner private props or when a required machine field is missing
    or malformed — the recheck poll treats None as "not a block I recheck" and
    moves on. The poll only rechecks arrival-anchored legs, so a block whose
    private props carry no usable `arrive_by`/`baseline`/endpoints is dropped.

    Schema version (per `coding-policy: stateful-artifacts`): a record stamped
    with a `drive_planner_schema_version` NEWER than this tile supports reads as
    None — no-usable-prior-state, the safe non-disruptive fallback (the poll
    skips it rather than mis-parsing a future shape). A missing version is
    treated as v1 for back-compat; v1 is the only version today.
    """
    if not isinstance(event, dict):
        return None
    private = _private_props(event)

    version = _parse_int(private.get(KEY_SCHEMA_VERSION))
    if version is not None and version > BLOCK_SCHEMA_VERSION:
        return None

    meeting_id = private.get(KEY_MEETING)
    if not isinstance(meeting_id, str) or not meeting_id:
        return None

    baseline = _parse_int(private.get(KEY_BASELINE))
    arrive_by = _parse_iso(private.get(KEY_ARRIVE_BY))
    origin = private.get(KEY_ORIGIN)
    destination = private.get(KEY_DESTINATION)
    direction = private.get(KEY_DIRECTION)
    if baseline is None or baseline < 0 or arrive_by is None:
        return None
    if not isinstance(origin, str) or not origin:
        return None
    if not isinstance(destination, str) or not destination:
        return None
    if direction not in ("outbound", "return", "bridge"):
        return None

    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None

    calendar_id = event.get("calendar_id")
    return BlockState(
        event_id=event_id,
        calendar_id=calendar_id if isinstance(calendar_id, str) and calendar_id else None,
        meeting_id=meeting_id,
        direction=direction,
        baseline_seconds=baseline,
        arrive_by=arrive_by,
        origin=origin,
        destination=destination,
        alerted=parse_alerted(private.get(KEY_ALERTED)),
    )
