"""Tests for the flight-assist managed-event tag codec (`calendar_tags.py`).

The tags ride in the event description's `<!--fa:{...}-->` comment (the live v3
Composio toolkit has no writable extendedProperties). These tests pin the round
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

from calendar_tags import decode_tags, encode_tags, strip_tags  # noqa: E402

TAGS = {"faFlightId": "100", "faKind": "boarding", "faManaged": "created"}


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
