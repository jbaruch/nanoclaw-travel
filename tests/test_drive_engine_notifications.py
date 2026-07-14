"""Tests for the operator-notification gating (#171 follow-up).

Covers the three deterministic pieces: `material_update_delta` (what counts as a
drive-time swing worth alerting), `apply_plan`'s recording of notification
material for APPLIED ops only, and `build_sweep_payload`'s per-meeting grouping +
wake gating (wake only on a skippable meeting add or a material re-time; silent on
removes, airport adds, converts, and routine re-times).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import GEN_UNIFIED, ParsedBlock  # noqa: E402
from calendar_apply import ApplyResult, apply_plan  # noqa: E402
from reconcile import (  # noqa: E402
    Create,
    DesiredBlock,
    ReconcilePlan,
    Update,
    material_update_delta,
    plan_reconcile,
)
from reconcile_sweep import build_sweep_payload  # noqa: E402

UTC = timezone.utc


def _dt(h, mi=0):
    return datetime(2020, 7, 13, h, mi, tzinfo=UTC)


def _desired(identity="m1", kind="meeting_return", baseline=900, summary="Drive: Massage"):
    return DesiredBlock(
        identity=identity,
        kind=kind,
        summary=summary,
        start=_dt(10),
        end=_dt(10, 30),
        origin="Home",
        destination="Venue",
        baseline_seconds=baseline,
        anchor=_dt(10, 35),
        timezone="America/Chicago",
    )


class FakeComposio:
    def create_event(self, args):
        return {"id": "new"}

    def patch_event(self, args):
        pass

    def delete_event(self, args):
        pass


# --- material_update_delta --------------------------------------------------


def test_longer_drive_is_leave_sooner():
    assert material_update_delta(600, 900) == (5, "sooner")  # +5 min, +50%


def test_shorter_drive_is_leave_later():
    assert material_update_delta(600, 300) == (5, "later")  # -5 min, -50%


def test_at_patch_tolerance_and_material_fraction_alerts():
    # 120s = the patch gate (so it actually reaches apply_plan) AND 20% — material.
    assert material_update_delta(600, 720) == (2, "sooner")


def test_ten_percent_below_patch_tolerance_is_silent():
    # 60s IS 10%, but < the 120s patch gate — _needs_update schedules no Update,
    # so it never reaches apply_plan; alerting on it would promise a heads-up the
    # reconcile can't deliver. Silent. (See test_boundary_alert_reaches_apply_plan.)
    assert material_update_delta(600, 660) is None


def test_below_ten_percent_is_silent():
    assert material_update_delta(600, 630) is None  # +30s = 5%


def test_large_absolute_but_under_ten_percent_is_silent():
    # 150s change (>= patch tolerance, so it patches) on a 2000s drive is 7.5% —
    # not a proportionally material swing, so it patches silently.
    assert material_update_delta(2000, 2150) is None


def test_missing_or_zero_prior_is_never_material():
    assert material_update_delta(None, 900) is None
    assert material_update_delta(0, 900) is None


# --- boundary: the alert threshold agrees with the reconcile patch gate ------


def _current(baseline, identity="m1"):
    """A current unified block that differs from `_desired` ONLY in drive time."""
    return ParsedBlock(
        generation=GEN_UNIFIED,
        event_id="e1",
        identity=identity,
        kind="meeting_return",
        baseline_seconds=baseline,
        anchor=_dt(10, 35),
        origin="Home",
        destination="Venue",
    )


def test_boundary_alert_reaches_apply_plan():
    # A 120s swing IS the patch gate: plan_reconcile emits an Update carrying the
    # prior baseline, and apply_plan records the alert — the full production path,
    # not just the helper in isolation.
    plan = plan_reconcile([_desired("m1", "meeting_return", 720)], [_current(600)])
    assert len(plan.updates) == 1
    assert plan.updates[0].prior_baseline_seconds == 600
    result = apply_plan(plan, composio=FakeComposio(), calendar_id="primary")
    assert [(u["minutes"], u["direction"]) for u in result.material_updates] == [(2, "sooner")]


def test_sub_tolerance_change_produces_no_update_and_no_alert():
    # A 60s swing is below the patch gate, so plan_reconcile emits NO update — it
    # never reaches apply_plan, so there is nothing to alert. The alert floor and
    # the patch gate agree, so a "material" claim never outruns the reconcile.
    plan = plan_reconcile([_desired("m1", "meeting_return", 660)], [_current(600)])
    assert plan.updates == ()
    result = apply_plan(plan, composio=FakeComposio(), calendar_id="primary")
    assert result.material_updates == []


# --- apply_plan records notification material for APPLIED ops only -----------


def test_meeting_create_recorded_airport_create_not():
    plan = ReconcilePlan(
        creates=(
            Create(_desired("mtg", "meeting_return", 900, "Drive: Massage")),
            Create(_desired("flt", "airport_departure", 1200, "Drive: STN")),
        )
    )
    result = apply_plan(plan, composio=FakeComposio(), calendar_id="primary")
    assert result.created == 2
    # only the MEETING add is a notification (airport drives aren't skippable)
    assert [leg["meeting"] for leg in result.added_meeting_legs] == ["Massage"]


def test_material_update_recorded_routine_update_not():
    plan = ReconcilePlan(
        updates=(
            Update("e1", _desired("mtg1", "meeting_return", 900), prior_baseline_seconds=600),
            Update("e2", _desired("mtg2", "meeting_return", 630), prior_baseline_seconds=600),
        )
    )
    result = apply_plan(plan, composio=FakeComposio(), calendar_id="primary")
    assert result.updated == 2  # both patched (calendar stays accurate)
    assert len(result.material_updates) == 1  # only the +50% one alerts
    alert = result.material_updates[0]
    assert (alert["meeting"], alert["minutes"], alert["direction"]) == ("Massage", 5, "sooner")


def test_deferred_update_is_not_recorded():
    # Budget 0 defers every write — nothing applied, so nothing to notify.
    plan = ReconcilePlan(
        updates=(Update("e1", _desired("m", "meeting_return", 900), prior_baseline_seconds=600),)
    )
    result = apply_plan(plan, composio=FakeComposio(), calendar_id="primary", budget_seconds=0.0)
    assert result.deferred == 1
    assert result.material_updates == []


# --- build_sweep_payload: grouping + wake gating ----------------------------


def _legs(*entries):
    return [{"identity": i, "meeting": m, "when": w, "anchor": a} for (i, m, w, a) in entries]


def test_meeting_legs_grouped_one_per_meeting_earliest_anchor():
    applied = ApplyResult()
    applied.added_meeting_legs = _legs(
        ("mtgA", "Massage", "Sat Jul 18, 10:35", "2020-07-18T15:35:00+00:00"),  # return
        ("mtgA", "Massage", "Sat Jul 18, 09:50", "2020-07-18T14:50:00+00:00"),  # outbound (earlier)
        ("mtgB", "Dentist", "Mon Jul 20, 08:00", "2020-07-20T13:00:00+00:00"),
    )
    payload = build_sweep_payload(applied, [])
    added = payload["data"]["added_meeting_drives"]
    assert added == [
        {"meeting": "Massage", "when": "Sat Jul 18, 09:50"},  # earliest leg wins, chronological
        {"meeting": "Dentist", "when": "Mon Jul 20, 08:00"},
    ]
    assert payload["wake_agent"] is True


def test_wake_false_on_removes_and_routine_only():
    applied = ApplyResult(created=0, updated=3, deleted=4, converted=1)  # counts only, no notifs
    payload = build_sweep_payload(applied, ["skipped a leg"])
    assert payload["wake_agent"] is False
    assert payload["data"]["added_meeting_drives"] == []
    assert payload["data"]["material_updates"] == []
    assert payload["data"]["applied"]["deleted"] == 4  # still reported in counts


def test_wake_true_on_material_update_alone():
    applied = ApplyResult(updated=1)
    applied.material_updates = [
        {
            "identity": "m",
            "meeting": "Massage",
            "minutes": 5,
            "direction": "sooner",
            "when": "Sat Jul 18, 10:35",
            "anchor": "2020-07-18T15:35:00+00:00",
        }
    ]
    payload = build_sweep_payload(applied, [])
    assert payload["wake_agent"] is True
    assert payload["data"]["material_updates"] == [
        {"meeting": "Massage", "minutes": 5, "direction": "sooner", "when": "Sat Jul 18, 10:35"}
    ]


def test_material_deduped_per_meeting_largest_swing():
    applied = ApplyResult(updated=2)
    applied.material_updates = [
        {
            "identity": "m",
            "meeting": "Massage",
            "minutes": 3,
            "direction": "sooner",
            "when": "a",
            "anchor": "2020-07-18T14:00:00+00:00",
        },
        {
            "identity": "m",
            "meeting": "Massage",
            "minutes": 8,
            "direction": "sooner",
            "when": "b",
            "anchor": "2020-07-18T16:00:00+00:00",
        },
    ]
    payload = build_sweep_payload(applied, [])
    material = payload["data"]["material_updates"]
    assert len(material) == 1 and material[0]["minutes"] == 8
