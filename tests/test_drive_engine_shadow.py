"""Tests for shadow-mode plan rendering (#156 R4).

Deterministic fixtures only — hand-built reconcile plans, no wall-clock. These pin
the count summary (the shadow acceptance surface) and the human-readable rendering,
including the no-op case.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from reconcile import (  # noqa: E402
    Convert,
    Create,
    Delete,
    DesiredBlock,
    ReconcilePlan,
    Update,
)
from shadow import plan_counts, render_plan  # noqa: E402

UTC = timezone.utc


def _desired(identity, kind="airport_departure"):
    a = datetime(2020, 7, 12, 8, tzinfo=UTC)
    return DesiredBlock(
        identity=identity,
        kind=kind,
        summary=f"Drive: {identity}",
        start=a,
        end=a,
        origin="Hotel",
        destination="APT",
        baseline_seconds=1800,
        anchor=a,
    )


def test_counts_separate_converts_from_deletes():
    plan = ReconcilePlan(
        creates=(Create(_desired("A")),),
        updates=(Update("e1", _desired("B")),),
        deletes=(Delete("d1", "orphan"), Delete("d2", "orphan")),
        converts=(Convert(_desired("C"), ("l1", "l2")),),
    )
    counts = plan_counts(plan)
    assert counts == {
        "creates": 1,
        "updates": 1,
        "deletes": 2,
        "converts": 1,
        "legacy_converted": 2,
    }


def test_render_noop():
    out = render_plan(ReconcilePlan())
    assert "no changes" in out
    assert "0 create" in out


def test_render_lists_each_action():
    plan = ReconcilePlan(
        creates=(Create(_desired("STN-CPH-20200712T0900Z")),),
        deletes=(Delete("cph1", "legacy orphan: no desired leg"),),
        converts=(Convert(_desired("BNA-STN-20200712T0600Z"), ("stn1", "stn2")),),
    )
    out = render_plan(plan)
    assert "+ CREATE airport_departure STN-CPH-20200712T0900Z" in out
    assert "- DELETE event cph1  (legacy orphan: no desired leg)" in out
    assert "⇄ CONVERT airport_departure BNA-STN-20200712T0600Z" in out
    assert "stn1, stn2" in out


def test_render_header_override():
    out = render_plan(ReconcilePlan(), header="[live] applying")
    assert out.startswith("[live] applying:")
