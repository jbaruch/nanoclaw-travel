"""Tests for the apply path (execute a reconcile plan on the calendar).

Deterministic fixtures only — a fake Composio client recording calls, hand-built
plans, fixed datetimes. These pin: deletes need only an id (the drive-planner
cleanup path), creates build local-tz args, converts create-then-delete-legacy,
updates recreate-then-delete with rollback on a failed delete, and a 404 delete is
treated as done.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from calendar_apply import ApplyResult, apply_plan, build_create_args  # noqa: E402
from composio_client import ComposioError  # noqa: E402
from reconcile import Convert, Create, Delete, DesiredBlock, ReconcilePlan, Update  # noqa: E402

UTC = timezone.utc


def _dt(h, mi=0):
    return datetime(2020, 7, 13, h, mi, tzinfo=UTC)


def _desired(identity="m1", kind="meeting_outbound", tz="America/Chicago"):
    return DesiredBlock(
        identity=identity,
        kind=kind,
        summary="Drive: Swimming Practice",
        start=_dt(8, 18),
        end=_dt(8, 45),
        origin="Home",
        destination="Pool",
        baseline_seconds=1620,
        anchor=_dt(8, 45),
        timezone=tz,
    )


class FakeComposio:
    def __init__(self, *, delete_404=(), delete_fail=()):
        self.created = []
        self.deleted = []
        self._delete_404 = set(delete_404)
        self._delete_fail = set(delete_fail)
        self._n = 0

    def create_event(self, args):
        self._n += 1
        eid = f"new{self._n}"
        self.created.append(args)
        return {"id": eid}

    def delete_event(self, args):
        eid = args["event_id"]
        if eid in self._delete_404:
            raise ComposioError("gone", status_code=404)
        if eid in self._delete_fail:
            raise ComposioError("boom")
        self.deleted.append(eid)


# --- create args (local tz) -------------------------------------------------


def test_create_args_render_in_local_tz():
    args = build_create_args(_desired(tz="America/Chicago"), calendar_id="primary")
    # 08:18 UTC in America/Chicago (CDT, -05:00) is 03:18 local
    assert args["timezone"] == "America/Chicago"
    assert args["start_datetime"].startswith("2020-07-13T03:18")
    assert args["transparency"] == "transparent"
    assert "[drive-engine:leg=m1:kind=meeting_outbound]" in args["description"]


def test_create_args_unknown_tz_falls_back_to_utc():
    args = build_create_args(_desired(tz="Not/AZone"), calendar_id="primary")
    assert args["timezone"] == "UTC"
    assert args["start_datetime"].startswith("2020-07-13T08:18")


# --- delete-only cleanup path ----------------------------------------------


def test_deletes_need_only_ids():
    plan = ReconcilePlan(deletes=(Delete("dp1", "orphan"), Delete("dp2", "orphan")))
    comp = FakeComposio()
    result = apply_plan(plan, composio=comp, calendar_id="primary")
    assert result.deleted == 2
    assert set(comp.deleted) == {"dp1", "dp2"}
    assert comp.created == []


def test_delete_404_counts_as_done():
    plan = ReconcilePlan(deletes=(Delete("gone1", "orphan"),))
    comp = FakeComposio(delete_404={"gone1"})
    result = apply_plan(plan, composio=comp, calendar_id="primary")
    assert result.deleted == 1  # 404 = already gone = success


def test_delete_failure_recorded_not_counted():
    plan = ReconcilePlan(deletes=(Delete("bad", "orphan"),))
    comp = FakeComposio(delete_fail={"bad"})
    result = apply_plan(plan, composio=comp, calendar_id="primary")
    assert result.deleted == 0
    assert any("bad" in e for e in result.errors)


# --- create / convert / update ---------------------------------------------


def test_create_builds_one_event():
    plan = ReconcilePlan(creates=(Create(_desired()),))
    comp = FakeComposio()
    result = apply_plan(plan, composio=comp, calendar_id="primary")
    assert result.created == 1 and len(comp.created) == 1


def test_convert_creates_new_and_deletes_all_legacy():
    plan = ReconcilePlan(converts=(Convert(_desired(), ("leg1", "leg2")),))
    comp = FakeComposio()
    result = apply_plan(plan, composio=comp, calendar_id="primary")
    assert result.converted == 1
    assert len(comp.created) == 1
    assert set(comp.deleted) == {"leg1", "leg2"}


def test_update_recreates_then_deletes_old():
    plan = ReconcilePlan(updates=(Update("old1", _desired()),))
    comp = FakeComposio()
    result = apply_plan(plan, composio=comp, calendar_id="primary")
    assert result.updated == 1
    assert len(comp.created) == 1
    assert comp.deleted == ["old1"]


def test_update_rolls_back_replacement_when_old_delete_fails():
    plan = ReconcilePlan(updates=(Update("old1", _desired()),))
    comp = FakeComposio(delete_fail={"old1"})
    result = apply_plan(plan, composio=comp, calendar_id="primary")
    # old delete failed → not counted as updated, and the new block is rolled back
    assert result.updated == 0
    assert "new1" in comp.deleted  # replacement deleted (rollback)
    assert any("old1" in e for e in result.errors)


def test_apply_result_totals():
    result = ApplyResult(created=1, updated=2, deleted=3, converted=4)
    assert result.total_writes == 10
