"""Tests for the apply path (execute a reconcile plan on the calendar).

Deterministic fixtures only — a fake calendar client recording calls, hand-built
plans, fixed datetimes. These pin: deletes need only an id (the drive-planner
cleanup path), creates build local-tz args, converts create-then-delete-legacy,
updates PATCH the same event in place (never recreate — so a kill can't duplicate,
#164), the wall-clock budget defers writes past its deadline, and a 404 delete is
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
from google_calendar_client import GoogleCalendarError  # noqa: E402
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


class FakeCalendar:
    def __init__(self, *, delete_404=(), delete_fail=(), patch_fail=()):
        self.created = []
        self.deleted = []
        self.patched = []
        self._delete_404 = set(delete_404)
        self._delete_fail = set(delete_fail)
        self._patch_fail = set(patch_fail)
        self._n = 0

    def create_event(self, args):
        self._n += 1
        eid = f"new{self._n}"
        self.created.append(args)
        return {"id": eid}

    def delete_event(self, args):
        eid = args["event_id"]
        if eid in self._delete_404:
            raise GoogleCalendarError("gone", status_code=404)
        if eid in self._delete_fail:
            raise GoogleCalendarError("boom")
        self.deleted.append(eid)

    def patch_event(self, args):
        eid = args["event_id"]
        if eid in self._patch_fail:
            raise GoogleCalendarError("boom")
        self.patched.append(args)


# --- create args (local tz) -------------------------------------------------


def test_create_args_render_in_local_tz():
    args = build_create_args(_desired(tz="America/Chicago"), calendar_id="primary")
    # 08:18 UTC in America/Chicago (CDT, -05:00) is 03:18 local
    assert args["start"]["timeZone"] == "America/Chicago"
    assert args["end"]["timeZone"] == "America/Chicago"
    assert args["start"]["dateTime"].startswith("2020-07-13T03:18")
    assert "[drive-engine:leg=m1:kind=meeting_outbound]" in args["description"]


def test_create_args_block_is_busy():
    # A drive is time the operator is unavailable — scheduling tools must not book
    # over it. CREATE is the only path that can set this (PATCH takes no
    # `transparency`), so every block is stamped Busy at create time.
    assert build_create_args(_desired(), calendar_id="primary")["transparency"] == "opaque"


def test_create_args_block_is_tangerine():
    # #167: every drive block is stamped Tangerine (colorId "6", the only orange)
    # so it reads as visually distinct from meetings and flights on the calendar.
    assert build_create_args(_desired(), calendar_id="primary")["colorId"] == "6"


def test_create_args_unknown_tz_falls_back_to_utc():
    args = build_create_args(_desired(tz="Not/AZone"), calendar_id="primary")
    # UTC is a real IANA name, so it is always safe to declare as timeZone.
    assert args["start"]["timeZone"] == "UTC"
    assert args["start"]["dateTime"].startswith("2020-07-13T08:18")


def test_create_args_add_no_self_attendee_so_block_shows_accepted():
    """#158: a drive block is a personal event, not an invite.

    Composio injected the connected user as a `needsAction` self-attendee, so
    the block rendered as an unconfirmed invite until an `exclude_organizer:
    true` flag suppressed it. events.insert adds no attendees at all, so the
    block shows as accepted with nothing to suppress — and the flag must not
    linger in the body, where Calendar would reject it as unknown.
    """
    args = build_create_args(_desired(), calendar_id="primary")
    assert "attendees" not in args
    assert "exclude_organizer" not in args


# --- delete-only cleanup path ----------------------------------------------


def test_deletes_need_only_ids():
    plan = ReconcilePlan(deletes=(Delete("dp1", "orphan"), Delete("dp2", "orphan")))
    comp = FakeCalendar()
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.deleted == 2
    assert set(comp.deleted) == {"dp1", "dp2"}
    assert comp.created == []


def test_delete_404_counts_as_done():
    plan = ReconcilePlan(deletes=(Delete("gone1", "orphan"),))
    comp = FakeCalendar(delete_404={"gone1"})
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.deleted == 1  # 404 = already gone = success


def test_delete_failure_recorded_not_counted():
    plan = ReconcilePlan(deletes=(Delete("bad", "orphan"),))
    comp = FakeCalendar(delete_fail={"bad"})
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.deleted == 0
    assert any("bad" in e for e in result.errors)


# --- create / convert / update ---------------------------------------------


def test_create_builds_one_event():
    plan = ReconcilePlan(creates=(Create(_desired()),))
    comp = FakeCalendar()
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.created == 1 and len(comp.created) == 1


def test_convert_creates_new_and_deletes_all_legacy():
    plan = ReconcilePlan(converts=(Convert(_desired(), ("leg1", "leg2")),))
    comp = FakeCalendar()
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.converted == 1
    assert len(comp.created) == 1
    assert set(comp.deleted) == {"leg1", "leg2"}


def test_convert_rolls_back_new_when_a_legacy_delete_fails():
    # If any legacy block survives, new + legacy would duplicate — roll back the
    # new block and don't count the convert (retried next cycle).
    plan = ReconcilePlan(converts=(Convert(_desired(), ("leg1", "leg2")),))
    comp = FakeCalendar(delete_fail={"leg2"})
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.converted == 0
    assert "leg1" in comp.deleted  # the deletable legacy was still removed
    assert "new1" in comp.deleted  # replacement rolled back (no duplicate left)
    assert any("leg2" in e for e in result.errors)


def test_update_patches_in_place_never_duplicates():
    """#164: an update is a single in-place PATCH of the same event — no create,
    no delete, so a kill right after can't leave a duplicate."""
    plan = ReconcilePlan(updates=(Update("old1", _desired()),))
    comp = FakeCalendar()
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.updated == 1
    assert comp.created == []  # never recreated
    assert comp.deleted == []  # never deletes the old block
    assert len(comp.patched) == 1
    assert comp.patched[0]["event_id"] == "old1"  # patched the SAME event
    # #167: the shift re-asserts Tangerine, so a block created before the colour
    # landed is recoloured in place with no separate backfill pass.
    assert comp.patched[0]["colorId"] == "6"


def test_update_patch_failure_is_recorded_not_counted():
    plan = ReconcilePlan(updates=(Update("old1", _desired(identity="m1")),))
    comp = FakeCalendar(patch_fail={"old1"})
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.updated == 0
    assert comp.created == [] and comp.deleted == []  # nothing left behind
    assert any("m1" in e for e in result.errors)


def test_update_with_no_event_id_is_skipped():
    plan = ReconcilePlan(updates=(Update(None, _desired()),))
    comp = FakeCalendar()
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.updated == 0
    assert comp.patched == []
    assert any("no event_id" in e for e in result.errors)


class _Clock:
    """Deterministic monotonic: returns start, start+step, start+2*step, ... on
    each call — so `over_budget` flips predictably at a known op count."""

    def __init__(self, start=0.0, step=1.0):
        self.t = start
        self.step = step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def test_budget_defers_writes_past_the_deadline():
    """#164: apply stops starting new ops once the wall-clock budget elapses,
    counting the rest as deferred (drained next sweep) instead of running past the
    host kill. Clock advances 1s/call, budget 2.5s → 2 creates then defer."""
    plan = ReconcilePlan(creates=tuple(Create(_desired(identity=f"m{i}")) for i in range(5)))
    comp = FakeCalendar()
    result = apply_plan(
        plan, calendar=comp, calendar_id="primary", budget_seconds=2.5, monotonic=_Clock()
    )
    assert result.created == 2
    assert result.deferred == 3
    assert len(comp.created) == 2


def test_budget_runs_deletes_before_creates():
    """Deletes (dedup/orphan cleanup) get budget priority — they run first, so a
    duplicate backlog drains even when the sweep can't also create this cycle."""
    plan = ReconcilePlan(
        deletes=(Delete("d1", "orphan"), Delete("d2", "orphan")),
        creates=(Create(_desired(identity="m1")), Create(_desired(identity="m2"))),
    )
    comp = FakeCalendar()
    result = apply_plan(
        plan, calendar=comp, calendar_id="primary", budget_seconds=2.5, monotonic=_Clock()
    )
    assert result.deleted == 2
    assert result.created == 0
    assert result.deferred == 2
    assert set(comp.deleted) == {"d1", "d2"}


def test_no_budget_applies_everything():
    """Without a budget, apply runs the whole plan (unchanged default behavior)."""
    plan = ReconcilePlan(creates=tuple(Create(_desired(identity=f"m{i}")) for i in range(5)))
    comp = FakeCalendar()
    result = apply_plan(plan, calendar=comp, calendar_id="primary")
    assert result.created == 5
    assert result.deferred == 0


def test_apply_result_totals():
    result = ApplyResult(created=1, updated=2, deleted=3, converted=4)
    assert result.total_writes == 10
