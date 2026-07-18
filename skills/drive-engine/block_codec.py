"""Unified drive-block codec — marker, machine-state, and dual-source reader.

One codec for every drive block the engine writes, replacing the two legacy codecs
(flight-assist `<!--fadrive:-->` and drive-planner `<!--dp:-->`).

Where the machine state lives (the #178 migration)
--------------------------------------------------
Historically all state rode in the event **description** — the Composio v3 toolkit
this plugin shipped on exposed no writable `extendedProperties`, so the description
was the only field that round-tripped. The native Calendar API (nanoclaw#638) does
expose `extendedProperties.private`, so state is moving off the human-visible
description into that machine-only field. Every block already deployed carries its
state in the description, so the move is a live-data migration with a transition
window, not a field swap: the READER accepts BOTH before the writer flips (#178).

`parse_block` reads `extendedProperties.private` FIRST and the description SECOND —
whichever a block carries, it round-trips. Nothing writes `extendedProperties` yet
(the writer flip is a later phase), so today every live block still parses off its
description; the extended-properties branch lies dormant until the writer starts
emitting it. `build_extended_properties` is the schema's source of truth and the
writer's phase-2 target; `build_description` still produces the create/patch
description. The human line stays in the description on purpose — it is what the
operator actually sees in the calendar UI; only the machine state migrates.

Description shape: a human line, a self-marker, and a compact machine-state JSON
comment. Extended-properties shape: a flat `dengine_*`-namespaced string map (the
only value type `extendedProperties.private` accepts), one key per state field.

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

# --- extendedProperties.private state (the #178 migration target) -----------

# `extendedProperties.private` is a flat string→string map shared with any other
# tool that tags the event, so every key is `dengine_`-namespaced to avoid
# clobbering a neighbour's tag. Values are always strings (the only type the field
# accepts) — ints and datetimes are stringified on write and parsed back on read.
# The version key is spelled out (not abbreviated like the compact description
# keys) per `coding-policy: stateful-artifacts`, which requires every record to
# carry an auditable `schema_version` field by that name; the namespace prefix
# keeps it collision-safe in the shared map.
_EXT_KEY_VERSION = "dengine_schema_version"
_EXT_KEY_LEG = "dengine_leg"
_EXT_KEY_KIND = "dengine_kind"
_EXT_KEY_BASELINE = "dengine_b"
_EXT_KEY_ANCHOR = "dengine_a"
_EXT_KEY_WINDOW_END = "dengine_we"
_EXT_KEY_ORIGIN = "dengine_o"
_EXT_KEY_DESTINATION = "dengine_d"
_EXT_KEY_ALERTED = "dengine_al"

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


def build_extended_properties(
    *,
    identity: str,
    kind: str,
    baseline_seconds: int,
    anchor: datetime,
    origin: str,
    destination: str,
    window_end: datetime | None = None,
    alerted: frozenset | set = frozenset(),
) -> dict:
    """The `extendedProperties` body carrying the same machine state as the JSON
    comment — the #178 migration target and the schema's source of truth.

    Returns `{"private": {...}}`, the shape `events.insert` / `events.patch` take
    (Calendar merges the map into the event's existing private properties). Every
    value is a string — the only type `extendedProperties.private` accepts — so
    `baseline_seconds` and the datetimes are stringified here and parsed back in
    `parse_block`. `window_end` is emitted only for transfer legs, matching
    `build_description`.

    The reader (`parse_block`) consumes this today; the writer adopts it in the
    phase-2 flip. It carries no human line — the description keeps that, since it
    is what the operator sees; only the machine state moves here.
    """
    private: dict[str, str] = {
        _EXT_KEY_VERSION: str(UNIFIED_BLOCK_SCHEMA_VERSION),
        _EXT_KEY_LEG: identity,
        _EXT_KEY_KIND: kind,
        _EXT_KEY_BASELINE: str(baseline_seconds),
        _EXT_KEY_ANCHOR: anchor.isoformat(),
        _EXT_KEY_ORIGIN: origin,
        _EXT_KEY_DESTINATION: destination,
        _EXT_KEY_ALERTED: serialize_alerted(alerted),
    }
    if window_end is not None:
        private[_EXT_KEY_WINDOW_END] = window_end.isoformat()
    return {"private": private}


# --- parse (dual-source, three-shape reader) --------------------------------


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


def _parse_int(raw: object) -> int | None:
    """Parse a baseline that may be a real int (description JSON) or a numeric
    string (`extendedProperties`, which stores every value as a string)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _event_extended_private(event: object) -> dict | None:
    """The event's `extendedProperties.private` map, or None when absent/malformed."""
    if not isinstance(event, dict):
        return None
    ext = event.get("extendedProperties")
    if not isinstance(ext, dict):
        return None
    private = ext.get("private")
    return private if isinstance(private, dict) else None


def _parse_extended_block(private: dict, event_id: str | None) -> ParsedBlock | None:
    """Read a unified block off `extendedProperties.private`, or None to fall back.

    Returns None (not a malformed-but-identified block) when the map carries no
    current-version `dengine_*` state or lacks a usable leg identity — the caller
    then tries the description. A version other than the current one reads as "no
    unified state here" and falls back too, mirroring the description reader's
    unknown-version handling.
    """
    if private.get(_EXT_KEY_VERSION) != str(UNIFIED_BLOCK_SCHEMA_VERSION):
        return None
    identity = private.get(_EXT_KEY_LEG)
    kind = private.get(_EXT_KEY_KIND)
    if not (isinstance(identity, str) and identity and isinstance(kind, str) and kind):
        return None
    origin = private.get(_EXT_KEY_ORIGIN)
    destination = private.get(_EXT_KEY_DESTINATION)
    return ParsedBlock(
        generation=GEN_UNIFIED,
        event_id=event_id,
        identity=identity,
        kind=kind,
        baseline_seconds=_parse_int(private.get(_EXT_KEY_BASELINE)),
        anchor=_parse_dt(private.get(_EXT_KEY_ANCHOR)),
        window_end=_parse_dt(private.get(_EXT_KEY_WINDOW_END)),
        origin=origin if isinstance(origin, str) else None,
        destination=destination if isinstance(destination, str) else None,
        alerted=parse_alerted(private.get(_EXT_KEY_ALERTED)),
    )


def parse_block(event: object) -> ParsedBlock | None:
    """Read a drive block off a calendar event; dual-source, three-shape (#178, R4).

    Prefers `extendedProperties.private` (the #178 migration target), falling back
    to the description so a block written either way round-trips. Returns a
    ParsedBlock tagged with its generation, or None for an event that carries no
    unified extended-properties state AND none of the three description markers (or
    is malformed). Never raises — a single bad event must not abort a sweep.
    """
    eid = _event_id(event)

    private = _event_extended_private(event)
    if private is not None:
        extended = _parse_extended_block(private, eid)
        if extended is not None:
            return extended

    desc = _event_description(event)
    if desc is None:
        return None

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
