"""Tests for the reconcile diff (#156 G1 / G7 / R4).

Deterministic fixtures only — hand-built desired legs and parsed blocks, no
wall-clock. These pin the three collapses: an N-way duplicate storm reduces to one
kept block plus deletes; a suppressed connection's stale blocks and pre-filter
orphans are deleted (not merely left); prior-gen blocks matching a desired leg are
converted (create + delete legacy), including both halves of a dual-id codeshare.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import GEN_UNIFIED, ParsedBlock  # noqa: E402
from reconcile import (  # noqa: E402
    DesiredBlock,
    plan_reconcile,
)

UTC = UTC


def _dt(h, mi=0):
    return datetime(2020, 7, 12, h, mi, tzinfo=UTC)


def desired(identity, kind="airport_departure", *, anchor=None, origin="X"):
    anchor = anchor or _dt(8)
    return DesiredBlock(
        identity=identity,
        kind=kind,
        summary=f"Drive: {identity}",
        start=anchor - timedelta(minutes=30),
        end=anchor,
        origin=origin,
        destination="APT",
        baseline_seconds=1800,
        anchor=anchor,
    )


def unified_block(identity, kind="airport_departure", *, event_id, anchor=None, origin="X"):
    return ParsedBlock(
        generation=GEN_UNIFIED,
        event_id=event_id,
        identity=identity,
        kind=kind,
        anchor=anchor or _dt(8),
        origin=origin,
        destination="APT",
        baseline_seconds=1800,  # matches desired()'s baseline so "unchanged" is a no-op
    )


# --- create / update / noop -------------------------------------------------


def test_desired_with_no_current_is_a_create():
    plan = plan_reconcile([desired("STN-CPH-20200712T0900Z")], [])
    assert len(plan.creates) == 1
    assert plan.updates == () and plan.deletes == ()


def test_matching_unchanged_block_is_noop():
    ident = "STN-CPH-20200712T0900Z"
    plan = plan_reconcile(
        [desired(ident, anchor=_dt(8), origin="X")],
        [unified_block(ident, event_id="e1", anchor=_dt(8), origin="X")],
    )
    assert plan.is_noop


def test_changed_block_is_an_update():
    ident = "STN-CPH-20200712T0900Z"
    plan = plan_reconcile(
        [desired(ident, anchor=_dt(8, 30), origin="Hotel")],
        [unified_block(ident, event_id="e1", anchor=_dt(8), origin="X")],
    )
    assert len(plan.updates) == 1
    assert plan.updates[0].event_id == "e1"


def test_route_duration_change_alone_triggers_update():
    # Traffic grew: same anchor/origin/dest, but the routed drive changed. The
    # block's leave-by moved, so it must update — not silently keep a stale block.
    ident = "STN-CPH-20200712T0900Z"
    current = ParsedBlock(
        generation=GEN_UNIFIED,
        event_id="e1",
        identity=ident,
        kind="airport_departure",
        anchor=_dt(8),
        origin="X",
        destination="APT",
        baseline_seconds=1200,
    )
    d = DesiredBlock(
        identity=ident,
        kind="airport_departure",
        summary="Drive",
        start=_dt(7, 30),
        end=_dt(8),
        origin="X",
        destination="APT",
        baseline_seconds=1800,  # traffic grew: 20min -> 30min
        anchor=_dt(8),
    )
    plan = plan_reconcile([d], [current])
    assert len(plan.updates) == 1
    assert plan.updates[0].event_id == "e1"


# --- #309: an existing meeting block converges to the operator's zone ---------


def _meeting_desired(*, tz: str, from_operator: bool) -> DesiredBlock:
    return DesiredBlock(
        identity="mtg1",
        kind="meeting_outbound",
        summary="Drive: Baruch 1:1",
        start=_dt(7, 22),
        end=_dt(8),
        origin="Home",
        destination="Office",
        baseline_seconds=2280,
        anchor=_dt(8),
        timezone=tz,
        zone_from_operator=from_operator,
    )


def _meeting_block(*, tz: str | None) -> ParsedBlock:
    return ParsedBlock(
        generation=GEN_UNIFIED,
        event_id="e9",
        identity="mtg1",
        kind="meeting_outbound",
        anchor=_dt(8),
        origin="Home",
        destination="Office",
        baseline_seconds=2280,
        timezone=tz,
    )


def test_block_in_a_stale_zone_converges_to_the_operator_zone():
    """A block written in `Etc/GMT-2` off a stale source offset re-patches into
    the operator's zone once the reader supplies it; nothing else changed."""
    plan = plan_reconcile(
        [_meeting_desired(tz="America/Chicago", from_operator=True)],
        [_meeting_block(tz="Etc/GMT-2")],
    )
    assert [u.event_id for u in plan.updates] == ["e9"]


def test_fallback_zone_never_repatches_a_block():
    """Reader unavailable: the desired zone is the meeting's own fallback, and a
    difference from the block's zone must not flap the block."""
    plan = plan_reconcile(
        [_meeting_desired(tz="Etc/GMT-2", from_operator=False)],
        [_meeting_block(tz="America/Chicago")],
    )
    assert plan.is_noop


def test_block_already_in_the_operator_zone_is_a_noop():
    plan = plan_reconcile(
        [_meeting_desired(tz="America/Chicago", from_operator=True)],
        [_meeting_block(tz="America/Chicago")],
    )
    assert plan.is_noop


def test_block_whose_zone_was_not_read_back_is_a_noop():
    plan = plan_reconcile(
        [_meeting_desired(tz="America/Chicago", from_operator=True)],
        [_meeting_block(tz=None)],
    )
    assert plan.is_noop


def _baseline_block(identity, baseline_seconds):
    """A unified block matching desired() on everything but baseline_seconds."""
    return ParsedBlock(
        generation=GEN_UNIFIED,
        event_id="e1",
        identity=identity,
        kind="airport_departure",
        anchor=_dt(8),
        origin="X",
        destination="APT",
        baseline_seconds=baseline_seconds,
    )


def test_baseline_jitter_below_tolerance_is_noop():
    """#164: a sub-2-min routing-jitter swing in baseline_seconds is NOT an update.
    Comparing exactly made every sweep re-shift all legs, and a sweep killed
    mid-write duplicated them. desired baseline 1800, current off by 46s (observed
    jitter) → no-op."""
    ident = "STN-CPH-20200712T0900Z"
    plan = plan_reconcile(
        [desired(ident, anchor=_dt(8), origin="X")], [_baseline_block(ident, 1846)]
    )
    assert plan.is_noop


def test_baseline_change_at_tolerance_triggers_update():
    """A change of exactly the 120s tolerance (a real traffic shift, not jitter)
    still updates."""
    ident = "STN-CPH-20200712T0900Z"
    plan = plan_reconcile(
        [desired(ident, anchor=_dt(8), origin="X")], [_baseline_block(ident, 1800 - 120)]
    )
    assert len(plan.updates) == 1
    assert plan.updates[0].event_id == "e1"


# --- G1: duplicate storm collapses ------------------------------------------


def test_duplicate_identity_storm_keeps_one_deletes_rest():
    ident = "STN-CPH-20200712T0900Z"
    storm = [unified_block(ident, event_id=f"e{i}", anchor=_dt(8), origin="X") for i in range(7)]
    plan = plan_reconcile([desired(ident, anchor=_dt(8), origin="X")], storm)
    # first kept (unchanged → no update), other 6 deleted
    assert plan.updates == ()
    assert len(plan.deletes) == 6
    assert {d.event_id for d in plan.deletes} == {f"e{i}" for i in range(1, 7)}
    assert all(d.reason == "duplicate identity" for d in plan.deletes)


# --- G7: orphans deleted ----------------------------------------------------


def test_unified_orphan_is_deleted():
    # A suppressed connection's stale block: present on calendar, not desired.
    plan = plan_reconcile([], [unified_block("CPH-JFK-20200712T1300Z", event_id="orph")])
    assert len(plan.deletes) == 1
    assert plan.deletes[0].event_id == "orph"
    assert "orphan" in plan.deletes[0].reason


# --- R4: legacy convergence -------------------------------------------------
