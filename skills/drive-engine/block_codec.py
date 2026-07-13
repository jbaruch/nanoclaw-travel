"""Unified drive-block codec — marker, machine-state, and three-shape reader.

One codec for every drive block the engine writes, replacing the two legacy codecs
(flight-assist `<!--fadrive:-->` and drive-planner `<!--dp:-->`). All state rides in
the event description — the live calendar toolkit exposes no writable
extendedProperties — as three parts: a human line, a self-marker, and a compact
machine-state JSON comment.

Leg identity (the marker) follows #156 C1 / G4 and keys on the CANONICAL flight
identity, never the designator:

    airport_departure / airport_arrival : <dep>-<arr>-<sched_dep_utc>   (+ kind)
    airport_transfer                    : <arr-flight-key>|<dep-flight-key>
    meeting                             : the calendar meeting id

so a codeshare tracked under two designators still maps to one block. `kind`
disambiguates the departure vs arrival block on the same flight.

Three-shape reader (#156 R4 cutover): `parse_block` recognizes the new
`<!--dengine:-->` shape AND both legacy shapes, tagging each with its generation so
the reconcile can converge prior-gen blocks (adopt-under-new-identity + delete
legacy) and delete orphans, never double-stamp. Malformed / unrecognized events
parse to None (never raise), so one bad event cannot abort a sweep.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from chain import LegKind
from flight_identity import MergedFlight
from leg_anchor import ConcreteLeg

# --- schema + generations ---------------------------------------------------

UNIFIED_BLOCK_SCHEMA_VERSION = 1

GEN_UNIFIED = "unified"
GEN_LEGACY_FADRIVE = "legacy_fadrive"
GEN_LEGACY_DP = "legacy_dp"

# --- new unified marker + state ---------------------------------------------

_MARKER_TEMPLATE = "[drive-engine:leg={identity}:kind={kind}]"
_MARKER_RE = re.compile(r"\[drive-engine:leg=(?P<id>[^:\]]+):kind=(?P<kind>[^:\]]+)\]")
_STATE_RE = re.compile(r"<!--dengine:(?P<json>\{.*?\})-->", re.DOTALL)

_KEY_VERSION = "schema_version"
_KEY_BASELINE = "b"
_KEY_ANCHOR = "a"
_KEY_WINDOW_END = "we"
_KEY_ORIGIN = "o"
_KEY_DESTINATION = "d"
_KEY_ALERTED = "al"

ALERT_GROWTH = "growth"
ALERT_LEAVE_NOW = "leave_now"
_ALERT_VALUES = (ALERT_GROWTH, ALERT_LEAVE_NOW)

# --- legacy markers (read-only, for cutover convergence) --------------------

_LEGACY_FADRIVE_MARKER_RE = re.compile(
    r"\[flight-assist:flight=(?P<id>[^:\]]+):dir=(?P<dir>[^:\]]+)\]"
)
_LEGACY_FADRIVE_STATE_RE = re.compile(r"<!--fadrive:(?P<json>\{.*?\})-->", re.DOTALL)
_LEGACY_DP_MARKER_RE = re.compile(r"\[drive-planner:meeting=(?P<id>[^:\]]+):dir=(?P<dir>[^:\]]+)\]")
_LEGACY_DP_STATE_RE = re.compile(r"<!--dp:(?P<json>\{.*?\})-->", re.DOTALL)


# --- leg identity -----------------------------------------------------------


def canonical_flight_key(flight: MergedFlight) -> str:
    """Stable per-flight identity string: route + scheduled-departure UTC instant.

    Excludes the designator (#156 W3): two codeshare records for one physical
    flight share this key. Minute precision — the identity tolerance lives in the
    merge, so by here the flight is already one canonical instant.
    """
    stamp = flight.scheduled_dep.strftime("%Y%m%dT%H%MZ")
    return f"{flight.dep_airport}-{flight.arr_airport}-{stamp}"


def leg_identity(leg: ConcreteLeg) -> str:
    """The marker identity for a concrete airport leg (#156 C1 / G4).

    Departure / arrival key on the single flight; transfer keys on the ordered
    pair `<arrival-flight>|<departure-flight>` so the between-flights block has its
    own identity distinct from either flight's own legs.
    """
    if leg.kind is LegKind.AIRPORT_TRANSFER:
        if leg.partner_flight is None:
            raise ValueError("transfer leg missing partner_flight for identity")
        return f"{canonical_flight_key(leg.flight)}|{canonical_flight_key(leg.partner_flight)}"
    return canonical_flight_key(leg.flight)


# --- alerts -----------------------------------------------------------------


def serialize_alerted(alerted: frozenset | set) -> str:
    """Serialize an alert set to the stable comma-joined record."""
    return ",".join(value for value in _ALERT_VALUES if value in alerted)


def parse_alerted(raw: object) -> frozenset:
    """Parse the comma-joined alert record into a set. Tolerant: unknown tokens
    dropped, non-string yields empty (a corrupt record must never crash a sweep)."""
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(token.strip() for token in raw.split(",") if token.strip() in _ALERT_VALUES)


# --- build ------------------------------------------------------------------


def build_marker(identity: str, kind: str) -> str:
    """The self-marker token for a block serving leg `identity` of `kind`."""
    if ":" in identity or "]" in identity:
        raise ValueError(f"leg identity must not contain ':' or ']': {identity!r}")
    return _MARKER_TEMPLATE.format(identity=identity, kind=kind)


def build_description(
    *,
    summary: str,
    identity: str,
    kind: str,
    baseline_seconds: int,
    anchor: datetime,
    origin: str,
    destination: str,
    window_end: datetime | None = None,
    alerted: frozenset | set = frozenset(),
) -> str:
    """The full block description: human line + self-marker + state JSON comment.

    Single source of the description format for both create and the
    suppression-record PATCH. `window_end` is set only for transfer legs (the
    other end of the window whose start is `anchor`).
    """
    state: dict[str, object] = {
        _KEY_VERSION: UNIFIED_BLOCK_SCHEMA_VERSION,
        _KEY_BASELINE: baseline_seconds,
        _KEY_ANCHOR: anchor.isoformat(),
        _KEY_ORIGIN: origin,
        _KEY_DESTINATION: destination,
        _KEY_ALERTED: serialize_alerted(alerted),
    }
    if window_end is not None:
        state[_KEY_WINDOW_END] = window_end.isoformat()
    marker = build_marker(identity, kind)
    blob = json.dumps(state, separators=(",", ":"))
    return f"{summary}\n{marker}\n<!--dengine:{blob}-->"


# --- parse (three-shape reader) ---------------------------------------------


@dataclass(frozen=True)
class ParsedBlock:
    """A drive block read off a calendar event, tagged with its generation.

    `generation` is `unified` for a new block, or a legacy tag the cutover
    reconcile uses to converge/delete. For unified blocks, `identity` + `kind`
    carry the leg identity and the state fields are populated. For legacy blocks,
    `legacy_id` + `legacy_direction` carry the old marker's identity so reconcile
    can match or orphan them; their state fields are best-effort.
    """

    generation: str
    event_id: str | None = None
    identity: str | None = None
    kind: str | None = None
    legacy_id: str | None = None
    legacy_direction: str | None = None
    baseline_seconds: int | None = None
    anchor: datetime | None = None
    window_end: datetime | None = None
    origin: str | None = None
    destination: str | None = None
    alerted: frozenset = field(default_factory=frozenset)


def _event_description(event: object) -> str | None:
    if not isinstance(event, dict):
        return None
    desc = event.get("description")
    return desc if isinstance(desc, str) else None


def _event_id(event: object) -> str | None:
    if isinstance(event, dict):
        eid = event.get("id")
        if isinstance(eid, str):
            return eid
    return None


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_block(event: object) -> ParsedBlock | None:
    """Read a drive block off a calendar event; recognize all three shapes (R4).

    Returns a ParsedBlock tagged with its generation, or None for an event that
    carries none of the three markers (or is malformed). Never raises — a single
    bad event must not abort a sweep.
    """
    desc = _event_description(event)
    if desc is None:
        return None
    eid = _event_id(event)

    unified_marker = _MARKER_RE.search(desc)
    if unified_marker:
        state: dict = {}
        state_match = _STATE_RE.search(desc)
        if state_match:
            try:
                state = json.loads(state_match["json"])
            except (ValueError, TypeError):
                state = {}
        if state.get(_KEY_VERSION) != UNIFIED_BLOCK_SCHEMA_VERSION:
            # Unknown version — treat as no usable prior state, but still identify
            # the block by its marker so reconcile can rewrite it.
            state = {}
        baseline = state.get(_KEY_BASELINE)
        return ParsedBlock(
            generation=GEN_UNIFIED,
            event_id=eid,
            identity=unified_marker["id"],
            kind=unified_marker["kind"],
            baseline_seconds=baseline if isinstance(baseline, int) else None,
            anchor=_parse_dt(state.get(_KEY_ANCHOR)),
            window_end=_parse_dt(state.get(_KEY_WINDOW_END)),
            origin=state.get(_KEY_ORIGIN) if isinstance(state.get(_KEY_ORIGIN), str) else None,
            destination=(
                state.get(_KEY_DESTINATION)
                if isinstance(state.get(_KEY_DESTINATION), str)
                else None
            ),
            alerted=parse_alerted(state.get(_KEY_ALERTED)),
        )

    fadrive = _LEGACY_FADRIVE_MARKER_RE.search(desc)
    if fadrive and _LEGACY_FADRIVE_STATE_RE.search(desc):
        return ParsedBlock(
            generation=GEN_LEGACY_FADRIVE,
            event_id=eid,
            legacy_id=fadrive["id"],
            legacy_direction=fadrive["dir"],
        )

    dp = _LEGACY_DP_MARKER_RE.search(desc)
    if dp and _LEGACY_DP_STATE_RE.search(desc):
        return ParsedBlock(
            generation=GEN_LEGACY_DP,
            event_id=eid,
            legacy_id=dp["id"],
            legacy_direction=dp["dir"],
        )

    return None
