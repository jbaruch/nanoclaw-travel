"""Tests for the reconcile diff (#156 G1 / G7 / R4).

Deterministic fixtures only — hand-built desired legs and parsed blocks, no
wall-clock. These pin the three collapses: an N-way duplicate storm reduces to one
kept block plus deletes; a suppressed connection's stale blocks and pre-filter
orphans are deleted (not merely left); prior-gen blocks matching a desired leg are
converted (create + delete legacy), including both halves of a dual-id codeshare.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import GEN_LEGACY_FADRIVE, GEN_UNIFIED, ParsedBlock  # noqa: E402
from reconcile import (  # noqa: E402
    DesiredBlock,
    legacy_keys_for_airport_leg,
    plan_reconcile,
)

UTC = timezone.utc


def _dt(h, mi=0):
    return datetime(2020, 7, 12, h, mi, tzinfo=UTC)


def desired(identity, kind="airport_departure", *, byair_ids=frozenset(), anchor=None, origin="X"):
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
        legacy_keys=legacy_keys_for_airport_leg(kind, byair_ids),
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
    )


def legacy_fadrive(flight_id, direction, *, event_id):
    return ParsedBlock(
        generation=GEN_LEGACY_FADRIVE,
        event_id=event_id,
        legacy_id=flight_id,
        legacy_direction=direction,
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


def test_legacy_orphan_is_deleted():
    # A pre-filter fadrive block for a flight that is no longer a desired leg.
    plan = plan_reconcile([], [legacy_fadrive("3358446", "to_airport", event_id="cph1")])
    assert len(plan.deletes) == 1
    assert plan.deletes[0].event_id == "cph1"
    assert "legacy orphan" in plan.deletes[0].reason


# --- R4: legacy convergence -------------------------------------------------


def test_legacy_block_matching_desired_is_converted():
    ident = "STN-CPH-20200712T0900Z"
    plan = plan_reconcile(
        [desired(ident, kind="airport_departure", byair_ids=frozenset({6277117}))],
        [legacy_fadrive("6277117", "to_airport", event_id="stn1")],
    )
    assert plan.creates == ()  # became a convert, not a plain create
    assert len(plan.converts) == 1
    assert plan.converts[0].legacy_event_ids == ("stn1",)


def test_codeshare_dual_id_converges_both_legacy_onto_one_leg():
    ident = "STN-CPH-20200712T0900Z"
    plan = plan_reconcile(
        [desired(ident, kind="airport_departure", byair_ids=frozenset({6277117, 7166978}))],
        [
            legacy_fadrive("6277117", "to_airport", event_id="fr"),
            legacy_fadrive("7166978", "to_airport", event_id="mw"),
        ],
    )
    assert len(plan.converts) == 1
    assert set(plan.converts[0].legacy_event_ids) == {"fr", "mw"}
    assert plan.deletes == ()  # both legacy consumed by the convert, none orphaned


def test_jul12_itinerary_shape():
    # BNA→STN→CPH→JFK: STN departure desired; CPH + JFK connections suppressed.
    # Current calendar: 5× STN legacy, 7× CPH legacy storm, 1× JFK legacy.
    stn_ident = "BNA-STN-20200712T0600Z"
    desired_legs = [desired(stn_ident, kind="airport_departure", byair_ids=frozenset({6277117}))]
    current = (
        [legacy_fadrive("6277117", "to_airport", event_id=f"stn{i}") for i in range(5)]
        + [legacy_fadrive("3358446", "to_airport", event_id=f"cph{i}") for i in range(7)]
        + [legacy_fadrive("3359520", "to_airport", event_id="jfk1")]
    )
    plan = plan_reconcile(desired_legs, current)
    # the 5 STN legacy converge onto the one STN leg
    assert len(plan.converts) == 1
    assert len(plan.converts[0].legacy_event_ids) == 5
    # the 7 CPH + 1 JFK have no desired leg → all deleted as legacy orphans
    assert len(plan.deletes) == 8
    assert all("legacy orphan" in d.reason for d in plan.deletes)
    assert plan.creates == ()
