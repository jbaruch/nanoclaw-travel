"""Tests for the flight-assist managed-event tag codec (`calendar_tags.py`).

The tags ride in the event description's `<!--fa:{...}-->` comment (the live v3
toolkit this plugin shipped on had no writable extendedProperties). These tests pin the round
trip the reconcile depends on: encode tags onto a (possibly pre-existing)
description, decode them back, and strip the comment so a byAir event's own
content survives an adopt without the tag accumulating on re-reads.

Synthetic fixtures only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from calendar_tags import (  # noqa: E402
    TAG_KEYS,
    decode_private_props,
    decode_tags,
    encode_tags,
    strip_tags,
)

TAGS = {"faFlightId": "100", "faKind": "boarding", "faManaged": "created"}


def _ext_event(private: dict, *, description: str | None = None) -> dict:
    """A fetched event carrying tags in extendedProperties.private."""
    event = {"id": "e1", "extendedProperties": {"private": private}}
    if description is not None:
        event["description"] = description
    return event


def test_encode_then_decode_round_trips():
    desc = encode_tags("", TAGS)
    assert decode_tags(desc) == TAGS


def test_encode_preserves_existing_description():
    desc = encode_tags("Gate B12", TAGS)
    assert desc.startswith("Gate B12")
    assert decode_tags(desc) == TAGS
    assert strip_tags(desc) == "Gate B12"


def test_encode_is_idempotent_does_not_accumulate():
    once = encode_tags("Gate B12", TAGS)
    twice = encode_tags(once, {"faFlightId": "200"})
    # Re-encoding strips the prior comment first — exactly one tag comment.
    assert twice.count("<!--fa:") == 1
    assert decode_tags(twice) == {"faFlightId": "200"}
    assert strip_tags(twice) == "Gate B12"


def test_empty_props_yields_clean_description_no_comment():
    assert encode_tags("Gate B12", {}) == "Gate B12"
    assert encode_tags("", {}) == ""


def test_decode_untagged_is_empty():
    assert decode_tags("just a normal description") == {}
    assert decode_tags("") == {}


def test_decode_malformed_json_is_empty():
    assert decode_tags("<!--fa:{not valid json}-->") == {}


def test_decode_non_string_is_empty():
    assert decode_tags(None) == {}
    assert decode_tags(42) == {}


def test_strip_non_string_is_empty_string():
    assert strip_tags(None) == ""


# --- dual-source reader: extendedProperties (the #178 migration) ------------


def test_decode_private_props_reads_extended_properties():
    assert decode_private_props(_ext_event(dict(TAGS))) == TAGS


def test_decode_private_props_prefers_extended_over_description():
    # An event carrying BOTH reads its tags from extendedProperties.
    ext = {"faFlightId": "999", "faKind": "flight", "faManaged": "adopted"}
    event = _ext_event(ext, description=encode_tags("Gate B12", TAGS))
    assert decode_private_props(event) == ext


def test_decode_private_props_falls_back_to_description():
    # No extendedProperties → read the description comment.
    event = {"id": "e1", "description": encode_tags("Gate B12", TAGS)}
    assert decode_private_props(event) == TAGS


def test_decode_private_props_partial_extended_falls_back_to_description():
    # A partial new-shape map (faManaged present but faFlightId missing) must NOT
    # shadow a valid legacy description tag — the description is the safe fallback
    # for incomplete new-shape data (coding-policy: stateful-artifacts).
    ext = {"faKind": "boarding", "faManaged": "created"}  # no faFlightId
    event = _ext_event(ext, description=encode_tags("Gate B12", TAGS))
    assert decode_private_props(event) == TAGS


def test_decode_private_props_incomplete_extended_with_no_description_is_empty():
    # Incomplete ext AND no usable description → "not managed", never a partial map.
    ext = {"faManaged": "created"}  # only the marker, nothing else
    assert decode_private_props(_ext_event(ext)) == {}


def test_decode_private_props_ignores_neighbour_private_keys():
    private = {**TAGS, "someOtherTool": "x"}
    assert decode_private_props(_ext_event(private)) == TAGS


def test_decode_private_props_untagged_is_empty():
    assert decode_private_props({"id": "e1", "description": "just a meeting"}) == {}
    assert decode_private_props({"id": "e1"}) == {}
    assert decode_private_props(None) == {}


def test_tag_keys_match_calendar_plan_constants():
    # Drift guard: calendar_tags owns the extendedProperties key names; they must
    # stay identical to calendar_plan's TAG_* the writer stamps.
    from calendar_plan import TAG_FLIGHT_ID, TAG_KIND, TAG_MANAGED

    assert TAG_KEYS == {TAG_FLIGHT_ID, TAG_KIND, TAG_MANAGED}
