"""Tests for the operator-notification gating (#171 follow-up).

Covers the three deterministic pieces: `material_update_delta` (what counts as a
drive-time swing worth alerting), `apply_plan`'s recording of notification
material for APPLIED ops only, and `build_sweep_payload`'s per-meeting grouping +
wake gating (wake only on a skippable meeting add or a material re-time; silent on
removes, airport adds, converts, and routine re-times).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

import reconcile as reconcile_module  # noqa: E402
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
from reconcile_sweep import build_sweep_payload, render_notification  # noqa: E402

UTC = UTC


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


class FakeCalendar:
    def create_event(self, args):
        return {"id": "new"}

    def patch_event(self, args):
        pass

    def delete_event(self, args):
        pass


def test_applied_roundtrip_notice_preserves_each_legs_charging_time():
    outbound = replace(_desired(kind="meeting_outbound"), charging_note="1 charging stop, ~30 min")
    returning = replace(_desired(), charging_note="1 charging stop, ~30 min")
    result = apply_plan(
        ReconcilePlan(creates=(Create(outbound), Create(returning))),
        calendar=FakeCalendar(),
        now=_dt(0),
        creation_clock=lambda: _dt(0),
    )
    payload = build_sweep_payload(result, [])
    message = payload["data"]["message"]
    assert "Outbound: 1 charging stop, ~30 min" in message
    assert "Return: 1 charging stop, ~30 min" in message


# --- material_update_delta --------------------------------------------------
#
# Alerting now needs three things at once: the drive is IMMINENT (within the
# horizon), the swing is BIG (>= the absolute floor), and it is PROPORTIONAL
# (>= the fraction). Everything else patches the calendar silently.

SOON = _dt(11)  # inside the horizon of NOW_APPLY below
NOW_APPLY = _dt(10)
FAR = NOW_APPLY + timedelta(days=60)


def _delta(prior, new, *, starts_at=SOON, now=NOW_APPLY):
    return material_update_delta(prior, new, starts_at=starts_at, now=now)


def test_longer_drive_is_leave_sooner():
    assert _delta(1800, 2700) == (15, "sooner")  # +15 min, +50%


def test_shorter_drive_is_leave_later():
    assert _delta(1800, 900) == (15, "later")  # -15 min, -50%


def test_a_two_month_out_change_is_silent_however_big():
    """The complaint that motivated the horizon: a drive that far out is
    re-routed dozens of times before it happens, and every swing was announced.
    Same swing that alerts when imminent."""
    assert _delta(1800, 2700, starts_at=FAR) is None
    assert _delta(1800, 2700) == (15, "sooner")


def test_a_drive_already_under_way_still_alerts():
    """Past the start is as imminent as it gets, not out of scope."""
    assert _delta(1800, 2700, starts_at=NOW_APPLY - timedelta(minutes=5)) == (15, "sooner")


def test_at_the_horizon_boundary_it_still_alerts():
    assert _delta(1800, 2700, starts_at=NOW_APPLY + timedelta(hours=2)) == (15, "sooner")


def test_just_past_the_horizon_it_is_silent():
    assert _delta(1800, 2700, starts_at=NOW_APPLY + timedelta(hours=2, seconds=1)) is None


def test_a_small_absolute_swing_is_silent_however_large_a_percentage():
    """The other half of the complaint: a 20-minute commute drifting 4 minutes
    is 20% and used to alert. Four minutes changes nothing anyone does."""
    assert _delta(1200, 1440) is None  # +4 min, +20%


def test_at_the_absolute_floor_it_alerts():
    assert _delta(3000, 3600) == (10, "sooner")  # +600s exactly, +20%


def test_just_below_the_absolute_floor_is_silent():
    assert _delta(3000, 3599) is None  # +599s, one second short


def test_large_absolute_but_under_ten_percent_is_silent():
    """A ten-minute swing on a two-hour drive is not a traffic event."""
    assert _delta(7200, 7800) is None  # +600s clears the floor, but is 8.3%


def test_missing_or_zero_prior_is_never_material():
    assert _delta(None, 900) is None
    assert _delta(0, 900) is None


def test_the_absolute_floor_never_drops_below_the_patch_gate():
    """Below the reconcile's patch tolerance no Update is scheduled at all, so
    the change never reaches apply_plan and the alert would promise a heads-up
    the sweep cannot deliver."""
    assert reconcile_module._MATERIAL_UPDATE_FLOOR_SECONDS >= (
        reconcile_module._BASELINE_SHIFT_TOLERANCE_SECONDS
    )


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


def test_an_alerting_swing_reaches_apply_plan():
    # The full production path, not the helper in isolation: plan_reconcile emits
    # an Update carrying the prior baseline and apply_plan records the alert.
    plan = plan_reconcile([_desired("m1", "meeting_return", 2700)], [_current(1800)])
    assert len(plan.updates) == 1
    assert plan.updates[0].prior_baseline_seconds == 1800
    result = apply_plan(
        plan,
        calendar=FakeCalendar(),
        calendar_id="primary",
        now=NOW_APPLY,
        creation_clock=lambda: _dt(0),
    )
    assert [(u["minutes"], u["direction"]) for u in result.material_updates] == [(15, "sooner")]


def test_a_swing_over_the_patch_gate_but_under_the_alert_floor_patches_silently():
    # 120s clears the reconcile's patch tolerance, so the calendar IS corrected —
    # it just no longer earns an interruption. This is the band the alert floor
    # opened up between "worth fixing" and "worth saying".
    plan = plan_reconcile([_desired("m1", "meeting_return", 720)], [_current(600)])
    assert len(plan.updates) == 1
    result = apply_plan(
        plan,
        calendar=FakeCalendar(),
        calendar_id="primary",
        now=NOW_APPLY,
        creation_clock=lambda: _dt(0),
    )
    assert result.updated == 1
    assert result.material_updates == []


def test_sub_tolerance_change_produces_no_update_and_no_alert():
    # A 60s swing is below the patch gate, so plan_reconcile emits NO update — it
    # never reaches apply_plan, so there is nothing to alert. The alert floor and
    # the patch gate agree, so a "material" claim never outruns the reconcile.
    plan = plan_reconcile([_desired("m1", "meeting_return", 660)], [_current(600)])
    assert plan.updates == ()
    result = apply_plan(
        plan, calendar=FakeCalendar(), calendar_id="primary", creation_clock=lambda: _dt(0)
    )
    assert result.material_updates == []


# --- apply_plan records notification material for APPLIED ops only -----------


def test_meeting_create_recorded_airport_create_not():
    plan = ReconcilePlan(
        creates=(
            Create(_desired("mtg", "meeting_return", 900, "Drive: Massage")),
            Create(_desired("flt", "airport_departure", 1200, "Drive: STN")),
        )
    )
    result = apply_plan(
        plan, calendar=FakeCalendar(), calendar_id="primary", creation_clock=lambda: _dt(0)
    )
    assert result.created == 2
    # only the MEETING add is a notification (airport drives aren't skippable)
    assert [leg["meeting"] for leg in result.added_meeting_legs] == ["Massage"]


def test_material_update_recorded_routine_update_not():
    plan = ReconcilePlan(
        updates=(
            Update("e1", _desired("mtg1", "meeting_return", 2700), prior_baseline_seconds=1800),
            Update("e2", _desired("mtg2", "meeting_return", 1830), prior_baseline_seconds=1800),
        )
    )
    result = apply_plan(
        plan,
        calendar=FakeCalendar(),
        calendar_id="primary",
        now=NOW_APPLY,
        creation_clock=lambda: _dt(0),
    )
    assert result.updated == 2  # both patched (calendar stays accurate)
    assert len(result.material_updates) == 1  # only the +15min one alerts
    alert = result.material_updates[0]
    assert (alert["meeting"], alert["minutes"], alert["direction"]) == ("Massage", 15, "sooner")


def test_a_far_future_update_patches_without_alerting():
    """End to end for the horizon: the calendar still gets the corrected drive
    time, the operator just isn't told about a change two months out."""
    far = _desired("mtg1", "meeting_return", 2700)
    far = replace(far, start=FAR, end=FAR + timedelta(minutes=45), anchor=FAR)
    plan = ReconcilePlan(updates=(Update("e1", far, prior_baseline_seconds=1800),))
    result = apply_plan(
        plan,
        calendar=FakeCalendar(),
        calendar_id="primary",
        now=NOW_APPLY,
        creation_clock=lambda: _dt(0),
    )
    assert result.updated == 1
    assert result.material_updates == []


def test_deferred_update_is_not_recorded():
    # Budget 0 defers every write — nothing applied, so nothing to notify.
    plan = ReconcilePlan(
        updates=(Update("e1", _desired("m", "meeting_return", 900), prior_baseline_seconds=600),)
    )
    result = apply_plan(
        plan,
        calendar=FakeCalendar(),
        calendar_id="primary",
        budget_seconds=0.0,
        creation_clock=lambda: _dt(0),
    )
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
    # #285: the sweep no longer wakes for a notice — the host delivers
    # `data.message` verbatim. "Has something to say" is now the message
    # being present, and the no-wake invariant is asserted alongside it.
    assert payload["data"]["message"]
    assert payload["wake_agent"] is False


def test_wake_false_on_removes_and_routine_only():
    applied = ApplyResult(created=0, updated=3, deleted=4, converted=1)  # counts only, no notifs
    payload = build_sweep_payload(applied, ["skipped a leg"])
    assert payload["wake_agent"] is False
    assert payload["data"]["added_meeting_drives"] == []
    assert payload["data"]["material_updates"] == []
    assert payload["data"]["applied"]["deleted"] == 4  # still reported in counts


def test_material_update_alone_produces_a_notice():
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
    # #285: the sweep no longer wakes for a notice — the host delivers
    # `data.message` verbatim. "Has something to say" is now the message
    # being present, and the no-wake invariant is asserted alongside it.
    assert payload["data"]["message"]
    assert payload["wake_agent"] is False
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


# --- render_notification: deterministic operator notice (#187) ---------------


def test_render_none_when_nothing_to_say():
    assert render_notification([], []) is None


def test_render_single_material_line():
    material = [{"meeting": "Massage", "minutes": 5, "direction": "sooner", "when": "Sat 10:35"}]
    assert (
        render_notification(material, [])
        == "Traffic: leave 5 min sooner for your Massage at Sat 10:35"
    )


def test_render_material_later_direction():
    material = [{"meeting": "Dentist", "minutes": 3, "direction": "later", "when": "Mon 08:00"}]
    assert render_notification(material, []) == (
        "Traffic: leave 3 min later for your Dentist at Mon 08:00"
    )


def test_render_multiple_material_lines_in_order():
    material = [
        {"meeting": "Massage", "minutes": 5, "direction": "sooner", "when": "Sat 10:35"},
        {"meeting": "Dentist", "minutes": 2, "direction": "later", "when": "Mon 08:00"},
    ]
    assert render_notification(material, []) == (
        "Traffic: leave 5 min sooner for your Massage at Sat 10:35\n"
        "Traffic: leave 2 min later for your Dentist at Mon 08:00"
    )


def test_render_single_added_drive():
    added = [{"meeting": "Massage", "when": "Sat 09:50"}]
    assert render_notification([], added) == (
        "Added a drive for Massage at Sat 09:50 — reply 'skip' if you're not driving to it."
    )


def test_render_several_added_drives_enumerated():
    added = [
        {"meeting": "Massage", "when": "Sat 09:50"},
        {"meeting": "Dentist", "when": "Mon 08:00"},
    ]
    assert render_notification([], added) == (
        "Added drives — reply 'skip 1', 'skip 2', or e.g. 'skip 1 and 2' "
        "for any you're not driving to:\n"
        "1. Massage at Sat 09:50\n"
        "2. Dentist at Mon 08:00"
    )


def test_render_material_then_added_combined():
    material = [{"meeting": "Massage", "minutes": 5, "direction": "sooner", "when": "Sat 10:35"}]
    added = [{"meeting": "Dentist", "when": "Mon 08:00"}]
    assert render_notification(material, added) == (
        "Traffic: leave 5 min sooner for your Massage at Sat 10:35\n"
        "Added a drive for Dentist at Mon 08:00 — reply 'skip' if you're not driving to it."
    )


def test_payload_carries_rendered_message_on_wake():
    applied = ApplyResult(updated=1)
    applied.material_updates = [
        {
            "identity": "m",
            "meeting": "Massage",
            "minutes": 5,
            "direction": "sooner",
            "when": "Sat 10:35",
            "anchor": "2020-07-18T15:35:00+00:00",
        }
    ]
    payload = build_sweep_payload(applied, [])
    # #285: the sweep no longer wakes for a notice — the host delivers
    # `data.message` verbatim. "Has something to say" is now the message
    # being present, and the no-wake invariant is asserted alongside it.
    assert payload["data"]["message"]
    assert payload["wake_agent"] is False
    assert payload["data"]["message"] == "Traffic: leave 5 min sooner for your Massage at Sat 10:35"


def test_payload_message_none_when_no_wake():
    applied = ApplyResult(created=0, updated=3, deleted=4, converted=1)
    payload = build_sweep_payload(applied, [])
    assert payload["wake_agent"] is False
    assert payload["data"]["message"] is None


# --- drive-or-fly question: the third wake reason (#231) ---------------------


_QUESTION = (
    "TN Tigers: no flight booked, and it's a 3h40m drive to Fairfield Inn. "
    "Reply 'drive' and I'll plan the drive, or 'fly' and I'll flag the missing flight."
)


def test_a_drive_or_fly_question_alone_produces_a_notice():
    """The operator owes an answer, so this is the one lodging-side event worth
    interrupting for."""
    payload = build_sweep_payload(ApplyResult(), [], [_QUESTION])
    # #285: the sweep no longer wakes for a notice — the host delivers
    # `data.message` verbatim. "Has something to say" is now the message
    # being present, and the no-wake invariant is asserted alongside it.
    assert payload["data"]["message"]
    assert payload["wake_agent"] is False
    assert payload["data"]["message"] == _QUESTION
    assert payload["data"]["drive_or_fly_questions"] == [_QUESTION]


def test_a_created_lodging_drive_stays_silent():
    """A lodging drive is not skippable — getting to the trip is the trip — so
    it applies like an airport drive: no wake, no message."""
    plan = ReconcilePlan(
        creates=(Create(_desired(identity="tn-tigers-2020-08", kind="lodging_outbound")),)
    )
    applied = apply_plan(
        plan, calendar=FakeCalendar(), calendar_id="primary", creation_clock=lambda: _dt(0)
    )
    payload = build_sweep_payload(applied, [])
    assert applied.created == 1
    assert applied.added_meeting_legs == []
    assert payload["wake_agent"] is False


def test_a_question_appends_below_the_drive_notices():
    """One notice, questions last — the operator reads what changed, then what
    is being asked of them."""
    applied = ApplyResult()
    applied.added_meeting_legs = _legs(
        ("mtgA", "Massage", "Sat Jul 18, 09:50", "2020-07-18T14:50:00+00:00"),
    )
    payload = build_sweep_payload(applied, [], [_QUESTION])
    lines = payload["data"]["message"].split("\n")
    assert lines[0].startswith("Added a drive for Massage")
    assert lines[-1] == _QUESTION


def test_no_questions_leaves_the_notice_unchanged():
    assert render_notification([], [], []) is None
    assert render_notification([], [], None) is None


def test_a_far_future_change_produces_no_notice_at_all():
    """The complaint end to end: two months out, nothing is sent. The calendar
    is still corrected — only the interruption is dropped."""
    far = _desired("mtg1", "meeting_return", 2700)
    far = replace(far, start=FAR, end=FAR + timedelta(minutes=45), anchor=FAR)
    plan = ReconcilePlan(updates=(Update("e1", far, prior_baseline_seconds=1800),))
    applied = apply_plan(
        plan,
        calendar=FakeCalendar(),
        calendar_id="primary",
        now=NOW_APPLY,
        creation_clock=lambda: _dt(0),
    )
    payload = build_sweep_payload(applied, [])
    assert applied.updated == 1
    assert payload["wake_agent"] is False
    assert payload["data"]["message"] is None


def test_the_same_change_inside_the_horizon_does_notify():
    """Same swing, near enough to act on — the operator hears about this one."""
    plan = ReconcilePlan(
        updates=(
            Update(
                "e1",
                _desired("mtg1", "meeting_return", 2700),
                prior_baseline_seconds=1800,
            ),
        )
    )
    applied = apply_plan(
        plan,
        calendar=FakeCalendar(),
        calendar_id="primary",
        now=NOW_APPLY,
        creation_clock=lambda: _dt(0),
    )
    payload = build_sweep_payload(applied, [])
    # #285: the sweep no longer wakes for a notice — the host delivers
    # `data.message` verbatim. "Has something to say" is now the message
    # being present, and the no-wake invariant is asserted alongside it.
    assert payload["data"]["message"]
    assert payload["wake_agent"] is False
    assert "leave 15 min sooner" in payload["data"]["message"]


# --- #285: the sweep never wakes; the host delivers the notice ---------------


def test_the_sweep_never_wakes_even_with_a_notice_to_deliver():
    """The invariant #285 rests on. `wake_agent` is False on every cadence
    sweep — including one with a notice — because the host sends
    `data.message` verbatim off the no-wake branch. Waking an agent to
    relay it is what let a Haiku container rewrite a drive-by-default
    line into a false binary."""
    payload = build_sweep_payload(ApplyResult(), [], [_QUESTION])
    assert payload["wake_agent"] is False
    assert payload["data"]["message"] == _QUESTION


def test_a_silent_sweep_carries_no_message_so_the_host_stays_quiet():
    """The other half: nothing to say means `message` is None, and the
    host's delivery branch requires a non-empty string. Same outcome the
    old wake gate produced, reached without a model."""
    payload = build_sweep_payload(ApplyResult(), [])
    assert payload["wake_agent"] is False
    assert payload["data"]["message"] is None
