"""Encode/decode a flight-assist airport drive block — the calendar IS the state.

The airport drive blocks (#90) carry the drive to/from an airport around a
flight. Like drive-planner's meeting blocks (Epic #59 §4), flight-assist keeps
no local store of them: the created calendar event itself carries everything a
recheck poll needs to re-evaluate it, read back by a direct API fetch. This
module is that codec.

It is deliberately a SELF-CONTAINED sibling of drive-planner's `block_props.py`,
not a shared extraction — the two codecs share a shape but differ in marker
namespace, directions, and anchor semantics, and a shared module would need
cross-skill mounting that isn't worth the coupling (#90 decision). drive-planner
is untouched.

State lives entirely in the event **description** (the live Composio v3 calendar
toolkit exposes no writable `extendedProperties`):

  * the human line (`Drive: → BNA (DL123)` / `Drive: BNA → home`),
  * the self-marker `[flight-assist:flight=<id>:dir=<to_airport|from_airport>]`,
  * a compact `<!--fa:{...}-->` JSON comment with the machine state the recheck
    poll reads back: schema version, baseline drive seconds, the anchor instant,
    the routed origin/destination, and the alert-suppression record. Parsed
    defensively — a malformed comment yields `None`, never raises.

Anchor semantics by direction (the `a` field):
  * `to_airport`  — the be-at-the-airport DEADLINE (`dep − clearance`). The block
    runs `[anchor − drive, anchor]`; you must LEAVE BY `anchor − drive`.
  * `from_airport`— the earliest the drive home can START (`actual_arr +
    post_arrival_delay`). The block runs `[anchor, anchor + drive]`.

The write contract (live v3): flat `start_datetime` + `event_duration_*`;
`location` carries the destination; `transparency` is "transparent" (Free) by
default (#90 decision); an explicit IANA `timezone` (the airport's) anchors the
block at the right instant (#83).

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from airport_block import build_block_args, build_description, parse_block, BlockState

    args = build_block_args(
        calendar_id="primary", flight_id="12345", direction="to_airport",
        summary="Drive: → BNA (DL123)", leg_start=leave_by, anchor=be_at_airport_by,
        baseline_seconds=1800, origin="<live location>", destination="BNA",
        timezone="America/Chicago",
    )
    state = parse_block(fetched_event)   # -> BlockState | None
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Self-marker stamped into the description so flight-assist recognizes its own
# airport drive blocks (idempotent create — never duplicate a block for the
# same flight+direction).
_MARKER_TEMPLATE = "[flight-assist:flight={flight_id}:dir={direction}]"
_MARKER_RE = re.compile(r"\[flight-assist:flight=(?P<id>[^:\]]+):dir=(?P<dir>[^:\]]+)\]")

DIRECTIONS = ("to_airport", "from_airport")

# Schema version of the calendar-as-state block record (per `coding-policy:
# stateful-artifacts`). Bump on any shape change to the description state JSON
# and add the owner-side upgrade in `parse_block`.
BLOCK_SCHEMA_VERSION = 1

# The machine state rides in an HTML comment so it stays out of the way in
# calendar UIs while remaining round-trippable. Short keys keep it compact.
_STATE_RE = re.compile(r"<!--fa:(?P<json>\{.*?\})-->", re.DOTALL)
# The version key is spelled out (`schema_version`), not abbreviated like the
# other keys: `coding-policy: stateful-artifacts` requires every record to
# carry an auditable `schema_version` field by that name.
_STATE_KEY_VERSION = "schema_version"
_STATE_KEY_BASELINE = "b"
_STATE_KEY_ANCHOR = "a"
_STATE_KEY_ORIGIN = "o"
_STATE_KEY_DESTINATION = "d"
_STATE_KEY_ALERTED = "al"

ALERT_GROWTH = "growth"
ALERT_LEAVE_NOW = "leave_now"
_ALERT_VALUES = (ALERT_GROWTH, ALERT_LEAVE_NOW)

# How far ahead of a to_airport block's leave-by the recheck poll starts
# evaluating it, and how long after it keeps evaluating. Black-box constants
# (per `coding-policy: script-as-black-box`); the poll overrides via kwargs.
DEFAULT_RECHECK_HORIZON_SECONDS = 45 * 60
DEFAULT_DEPARTED_GRACE_SECONDS = 15 * 60


def build_marker(flight_id: str, direction: str) -> str:
    """The self-marker token for a block serving `flight_id` in `direction`."""
    return _MARKER_TEMPLATE.format(flight_id=flight_id, direction=direction)


def parse_marker(text: object) -> tuple[str, str] | None:
    """Extract `(flight_id, direction)` from a description marker, or None."""
    if not isinstance(text, str):
        return None
    match = _MARKER_RE.search(text)
    return (match["id"], match["dir"]) if match else None


def serialize_alerted(alerted: frozenset | set) -> str:
    """Serialize an alert set to the stable comma-joined record."""
    return ",".join(value for value in _ALERT_VALUES if value in alerted)


def parse_alerted(raw: object) -> frozenset:
    """Parse the comma-joined alert record into a set.

    Unknown tokens are dropped; a non-string yields the empty set. Tolerant by
    design — a corrupt record must not crash the poll; at worst it re-sends an
    alert (annoying, not unsafe).
    """
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(token.strip() for token in raw.split(",") if token.strip() in _ALERT_VALUES)


def build_description(
    *,
    summary: str,
    flight_id: str,
    direction: str,
    baseline_seconds: int,
    anchor: datetime,
    origin: str,
    destination: str,
    alerted: frozenset | set = frozenset(),
) -> str:
    """The full block description: human line + self-marker + state JSON comment.

    The single source of the description format — `build_block_args` uses it for
    create, and the recheck poll re-runs it (with an updated `alerted`) to PATCH
    the suppression record back onto the event.
    """
    state = {
        _STATE_KEY_VERSION: BLOCK_SCHEMA_VERSION,
        _STATE_KEY_BASELINE: baseline_seconds,
        _STATE_KEY_ANCHOR: anchor.isoformat(),
        _STATE_KEY_ORIGIN: origin,
        _STATE_KEY_DESTINATION: destination,
        _STATE_KEY_ALERTED: serialize_alerted(alerted),
    }
    marker = build_marker(flight_id, direction)
    blob = json.dumps(state, separators=(",", ":"))
    return f"{summary}\n{marker}\n<!--fa:{blob}-->"


def _duration_minutes(leg_start: datetime, leg_end: datetime) -> int:
    """Whole-minute duration for the create call (always at least 1 minute)."""
    minutes = round((leg_end - leg_start).total_seconds() / 60)
    return max(minutes, 1)


def build_block_args(
    *,
    calendar_id: str,
    flight_id: str,
    direction: str,
    summary: str,
    leg_start: datetime,
    anchor: datetime,
    baseline_seconds: int,
    origin: str,
    destination: str,
    leg_end: datetime | None = None,
    busy: bool = False,
    timezone: str | None = None,
) -> dict:
    """Build the `GOOGLECALENDAR_CREATE_EVENT` arguments for an airport block.

    The live v3 contract: flat `start_datetime` + `event_duration_hour` /
    `event_duration_minutes`; `location` is the destination; the machine state
    rides in the description. Free (`transparency: "transparent"`) unless `busy`.

    Args:
        calendar_id: the calendar to create the block on (primary, per #90).
        flight_id: the served flight's byAir id (as a string).
        direction: "to_airport" or "from_airport".
        summary: the block's human title.
        leg_start: when the block starts. For to_airport this is the leave-by
            (`anchor − drive`); for from_airport it is the anchor itself.
        anchor: the deadline (to_airport: `dep − clearance`) or the earliest
            drive-home start (from_airport: `actual_arr + post_arrival_delay`).
            Stored in state; the poll re-derives leave-by from baseline + anchor.
        baseline_seconds: routed drive seconds captured at creation.
        origin / destination: the routed leg endpoints (the poll re-routes
            exactly this pair).
        leg_end: block end; defaults to `anchor` (correct for to_airport, where
            the drive ends at the deadline). from_airport passes `anchor + drive`.
        busy: create Busy instead of Free.
        timezone: the airport's IANA timezone; emitted as the CREATE `timezone`
            so the block lands at the right instant (#83). Omitted when None.

    Raises:
        ValueError: on a naive datetime, an empty endpoint, a negative or
            non-int baseline, an empty flight_id, or an unknown direction.
    """
    for label, value in (("leg_start", leg_start), ("anchor", anchor)):
        if value.tzinfo is None:
            raise ValueError(f"build_block_args: `{label}` must be timezone-aware")
    if leg_end is not None and leg_end.tzinfo is None:
        raise ValueError("build_block_args: `leg_end` must be timezone-aware")
    if not flight_id:
        raise ValueError("build_block_args: `flight_id` must be non-empty")
    if direction not in DIRECTIONS:
        raise ValueError(
            f"build_block_args: `direction` must be one of {DIRECTIONS} (got {direction!r})"
        )
    if not isinstance(baseline_seconds, int) or isinstance(baseline_seconds, bool):
        raise ValueError("build_block_args: `baseline_seconds` must be an int")
    if baseline_seconds < 0:
        raise ValueError("build_block_args: `baseline_seconds` must be non-negative")
    if not origin or not destination:
        raise ValueError("build_block_args: `origin` and `destination` must be non-empty")

    end = leg_end if leg_end is not None else anchor
    if end < leg_start:
        raise ValueError(
            "build_block_args: block end must not be before `leg_start` "
            "(a leg_end earlier than leg_start, or leg_start later than anchor)"
        )
    total_minutes = _duration_minutes(leg_start, end)
    description = build_description(
        summary=summary,
        flight_id=flight_id,
        direction=direction,
        baseline_seconds=baseline_seconds,
        anchor=anchor,
        origin=origin,
        destination=destination,
    )
    args = {
        "calendar_id": calendar_id,
        "summary": summary,
        "description": description,
        "location": destination,
        "start_datetime": leg_start.isoformat(),
        "event_duration_hour": total_minutes // 60,
        "event_duration_minutes": total_minutes % 60,
        "transparency": "opaque" if busy else "transparent",
    }
    if timezone:
        args["timezone"] = timezone
    return args


@dataclass(frozen=True)
class BlockState:
    """An airport drive block parsed back off a fetched calendar event.

    Carries exactly what the recheck poll needs to re-evaluate one block without
    a local store: the leg endpoints to re-route, the baseline to compare
    against, the anchor instant, which alerts already fired, and the summary (so
    the suppression patch can rebuild the full description).
    """

    event_id: str
    flight_id: str
    direction: str
    summary: str
    baseline_seconds: int
    anchor: datetime
    origin: str
    destination: str
    calendar_id: str | None = None
    alerted: frozenset = field(default_factory=frozenset)

    @property
    def baseline_leave_by(self) -> datetime:
        """When you must leave at the baseline drive time.

        Meaningful for `to_airport` (anchor is the deadline): `anchor − drive`.
        For `from_airport` the anchor IS the start, so this returns the anchor.
        """
        if self.direction == "from_airport":
            return self.anchor
        return self.anchor - timedelta(seconds=self.baseline_seconds)

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
            flight_id=self.flight_id,
            direction=self.direction,
            baseline_seconds=self.baseline_seconds,
            anchor=self.anchor,
            origin=self.origin,
            destination=self.destination,
            alerted=alerted,
        )


def next_alerts(
    alerted: frozenset | set, *, grew: bool, leave_now: bool
) -> tuple[tuple[str, ...], frozenset]:
    """Decide which alerts fire now and the suppression record after.

    The recheck gate reports `grew` (traffic grew past the threshold) and
    `leave_now` (the recomputed leave-by has arrived). Each fires AT MOST ONCE
    per block — re-pinging a still-grown drive every poll is the trust-eroding
    nag. Returns `(kinds_to_fire, new_alerted)`; `kinds_to_fire` is empty when
    both already fired (the poll then stays silent and patches nothing).
    """
    fire: list[str] = []
    if grew and ALERT_GROWTH not in alerted:
        fire.append(ALERT_GROWTH)
    if leave_now and ALERT_LEAVE_NOW not in alerted:
        fire.append(ALERT_LEAVE_NOW)
    new_alerted = frozenset(alerted) | frozenset(fire)
    return tuple(fire), new_alerted


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


def _decode_state(description: object) -> dict | None:
    """Pull the `<!--fa:{...}-->` state JSON out of a description, defensively."""
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

    Recognition is by the description's `<!--fa:{...}-->` state JSON plus the
    `[flight-assist:flight=...:dir=...]` marker. Returns None when the event
    carries no airport-block state, the marker is absent/malformed, or a
    required field is missing — the recheck poll treats None as "not a block I
    recheck" and moves on.

    Schema version (per `coding-policy: stateful-artifacts`): every record
    carries `schema_version`, and `parse_block` accepts ONLY the EXACT current
    version. v1 is the first version, so no owner-side migration exists yet — a
    missing, non-int, OLDER, or NEWER version all read as None
    (no-usable-prior-state, the safe non-disruptive fallback). When a future
    shape bumps the version, add the owner-side v1→vN upgrade here and widen
    acceptance accordingly.
    """
    if not isinstance(event, dict):
        return None
    description = event.get("description")
    state = _decode_state(description)
    if state is None:
        return None

    version = state.get(_STATE_KEY_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version != BLOCK_SCHEMA_VERSION:
        return None

    marker = _MARKER_RE.search(description) if isinstance(description, str) else None
    if marker is None:
        return None
    flight_id = marker["id"]
    direction = marker["dir"]
    if direction not in DIRECTIONS:
        return None

    baseline = state.get(_STATE_KEY_BASELINE)
    anchor = _parse_iso(state.get(_STATE_KEY_ANCHOR))
    origin = state.get(_STATE_KEY_ORIGIN)
    destination = state.get(_STATE_KEY_DESTINATION)
    if not isinstance(baseline, int) or isinstance(baseline, bool) or baseline < 0:
        return None
    if anchor is None:
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
        flight_id=flight_id,
        direction=direction,
        summary=summary if isinstance(summary, str) else "",
        baseline_seconds=baseline,
        anchor=anchor,
        origin=origin,
        destination=destination,
        alerted=parse_alerted(state.get(_STATE_KEY_ALERTED)),
    )
