"""Encode/decode flight-assist's managed-event tags in the event description.

flight-assist recognizes its own managed calendar events (boarding blocks it
created, byAir flight events it adopted) by a small tag map — `faFlightId`,
`faKind`, `faManaged`. The original design stamped these into
`extendedProperties.private`, but the Composio v3 toolkit this plugin shipped
on exposed
NO writable `extendedProperties` on any create/patch/update action (verified
against the NAS). The only durable, writable surface is the event
**description**, so the tags ride in a compact `<!--fa:{...}-->` JSON comment
appended to the human description.

The rest of the pipeline keeps working with the logical `private_props` dict:
`normalize_event` DECODES the comment back into `private_props` on read, and
the reconcile write helpers ENCODE `private_props` into the description on
create/patch. The human description (a byAir flight event's own content, when
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
