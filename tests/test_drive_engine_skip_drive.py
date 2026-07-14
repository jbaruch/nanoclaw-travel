"""Tests for the skip action (`skip_drive.py`).

`resolve_skip` is pure (raw events -> a skip target or same-name candidates) and
tested here without any Composio I/O. Deterministic fixtures: hand-built events
whose descriptions round-trip through the unified block codec.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import build_description  # noqa: E402
from skip_drive import SkipTarget, resolve_skip  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _anchor(day, h, mi=0):
    return datetime(2026, 7, day, h, mi, tzinfo=UTC)


def _event(
    eid, identity, *, meeting="Massage", kind="meeting_return", anchor=None, start_local=None
):
    anchor = anchor or _anchor(18, 15, 35)
    summary = f"Drive: {meeting}"
    desc = build_description(
        summary=summary,
        identity=identity,
        kind=kind,
        baseline_seconds=600,
        anchor=anchor,
        origin="Home",
        destination="Venue",
    )
    return {
        "id": eid,
        "summary": summary,
        "start": {"dateTime": start_local or "2026-07-18T10:35:00-05:00"},
        "description": desc,
    }


def test_unique_match_returns_target_with_all_leg_ids():
    # Outbound + return for the same meeting share identity and summary.
    events = [
        _event("out1", "mtgA", kind="meeting_outbound", anchor=_anchor(18, 15, 35)),
        _event("ret1", "mtgA", kind="meeting_return", anchor=_anchor(18, 17, 0)),
    ]
    target, candidates = resolve_skip(events, summary="Massage", now=NOW)
    assert candidates == []
    assert isinstance(target, SkipTarget)
    assert target.identity == "mtgA"
    assert set(target.event_ids) == {"out1", "ret1"}
    # expiry pads past the LATEST anchor (meeting end), not the earliest
    assert target.expires > _anchor(18, 17, 0)


def test_no_match_returns_nothing():
    events = [_event("x", "mtgA", meeting="Massage")]
    target, candidates = resolve_skip(events, summary="Dentist", now=NOW)
    assert target is None
    assert candidates == []


def test_same_name_meetings_are_ambiguous():
    # Two distinct meetings both named "Swimming Practice" -> hand back candidates.
    events = [
        _event("s1", "mon", meeting="Swimming Practice", start_local="2026-07-20T10:30:00-05:00"),
        _event("s2", "wed", meeting="Swimming Practice", start_local="2026-07-22T12:30:00-05:00"),
    ]
    target, candidates = resolve_skip(events, summary="Swimming Practice", now=NOW)
    assert target is None
    assert len(candidates) == 2
    assert all(c["meeting"] == "Swimming Practice" for c in candidates)
    assert {c["when"] for c in candidates} == {"Mon Jul 20, 10:30", "Wed Jul 22, 12:30"}


def test_airport_blocks_are_never_skippable():
    # An airport drive is not a skip target even if a summary somehow matched.
    events = [_event("a1", "flt", meeting="STN", kind="airport_departure")]
    target, candidates = resolve_skip(events, summary="STN", now=NOW)
    assert target is None
    assert candidates == []


def test_non_drive_events_ignored():
    events = [
        {"id": "plain", "summary": "Massage", "description": "just a meeting"},
        _event("d1", "mtgA", meeting="Massage"),
    ]
    target, _ = resolve_skip(events, summary="Massage", now=NOW)
    assert target is not None and target.identity == "mtgA"
    assert set(target.event_ids) == {"d1"}
