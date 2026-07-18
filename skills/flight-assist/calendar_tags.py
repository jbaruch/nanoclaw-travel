"""Encode/decode flight-assist's managed-event tags — dual-source reader.

flight-assist recognizes its own managed calendar events (boarding blocks it
created, byAir flight events it adopted) by a small tag map — `faFlightId`,
`faKind`, `faManaged`.

Where the tags live (the #178 migration)
----------------------------------------
The original design stamped these into `extendedProperties.private`, but the
Composio v3 toolkit this plugin shipped on exposed NO writable
`extendedProperties` on any create/patch/update action (verified against the
NAS). The only durable, writable surface was the event **description**, so the
tags ride in a compact `<!--fa:{...}-->` JSON comment appended to the human
description. The native Calendar API (nanoclaw#638) exposes
`extendedProperties.private`, so the tags are migrating back into that
machine-only field — the same live-data migration #178 ran for drive blocks:
the READER accepts BOTH before the writer flips.

`decode_private_props` reads `extendedProperties.private` FIRST and the
`<!--fa:-->` description comment SECOND, so an event tagged either way is
recognized. `normalize_event` calls it on read; the reconcile write helpers
still ENCODE into the description on create/adopt (the writer flip is a later
phase). The human description (a byAir flight event's own content, when
adopting) is preserved — the tag comment is appended, and stripped back off on
the next read so it never accumulates.

stdlib-only per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import json
import re

# The tag comment: hidden in most calendar UIs, round-trippable. A non-greedy
# body so a description with later HTML comments doesn't swallow them.
_TAG_RE = re.compile(r"\s*<!--fa:(?P<json>\{.*?\})-->", re.DOTALL)

# The managed-tag keys — also the keys under `extendedProperties.private` once
# the tags migrate off the description (#178). Already `fa`-namespaced, so they
# are collision-safe in the shared private map. Kept in sync with
# `calendar_plan`'s `TAG_*` constants by a test (drift guard).
TAG_KEYS = frozenset({"faFlightId", "faKind", "faManaged"})


def encode_tags(description: str, private_props: dict) -> str:
    """Append the tag comment to `description` (replacing any existing one).

    `description` is the human content to preserve (empty for a fresh block).
    An empty `private_props` yields the description unchanged (no tag comment) —
    an untagged event stays untagged.
    """
    clean = strip_tags(description)
    if not private_props:
        return clean
    blob = json.dumps(private_props, separators=(",", ":"), sort_keys=True)
    suffix = f"<!--fa:{blob}-->"
    return f"{clean}\n{suffix}" if clean else suffix


def strip_tags(description: object) -> str:
    """The human description with the `<!--fa:...-->` tag comment removed."""
    if not isinstance(description, str):
        return ""
    return _TAG_RE.sub("", description).rstrip()


def decode_tags(description: object) -> dict:
    """Pull the `private_props` map out of a description tag comment, defensively.

    Returns `{}` when there is no tag comment or it is malformed — an untagged
    or unreadable event is simply "not flight-assist-managed", never an error.
    """
    if not isinstance(description, str):
        return {}
    match = _TAG_RE.search(description)
    if match is None:
        return {}
    try:
        decoded = json.loads(match["json"])
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode_extended_tags(event: dict) -> dict | None:
    """The managed tags from `extendedProperties.private`, or None to fall back.

    Returns None unless the private map carries a COMPLETE managed-tag set (all
    of `TAG_KEYS` present as strings). A partial or malformed new-shape map must
    never shadow a valid legacy description tag — the description reader stays the
    safe fallback for incomplete new-shape data (`coding-policy:
    stateful-artifacts`). So `None` here also covers a map with no `fa*` tags at
    all (an event with only a neighbour tool's private keys); only the `fa*` tag
    keys are extracted, so a neighbour's keys are never returned.
    """
    ext = event.get("extendedProperties")
    if not isinstance(ext, dict):
        return None
    private = ext.get("private")
    if not isinstance(private, dict):
        return None
    tags = {k: v for k, v in private.items() if k in TAG_KEYS and isinstance(v, str)}
    return tags if TAG_KEYS <= tags.keys() else None


def decode_private_props(event: object) -> dict:
    """Read flight-assist's managed-tag `private_props`, dual-source (#178).

    Reads `extendedProperties.private` FIRST (the migration target), the
    description `<!--fa:-->` comment SECOND, so an event tagged either way is
    recognized. Returns `{}` for an untagged or malformed event — "not
    flight-assist-managed", never an error.
    """
    if isinstance(event, dict):
        extended = _decode_extended_tags(event)
        if extended is not None:
            return extended
        return decode_tags(event.get("description"))
    return {}
