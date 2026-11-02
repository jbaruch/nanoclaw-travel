"""Shadow-mode rendering of a reconcile plan — pure, no I/O.

Shadow / dry-run mode (#156 R4) computes the desired-vs-current diff and LOGS it
without writing, so the cutover can be validated against the production calendar
before any block is created or deleted. This module turns a `ReconcilePlan` into
that log: a stable count summary (the acceptance criterion — "the delete-diff
matches the counted garbage") and a human-readable line-per-action rendering.

Pure: formatting only. The I/O layer decides shadow-vs-live and emits the string.
"""

from __future__ import annotations

from reconcile import ReconcilePlan


def plan_counts(plan: ReconcilePlan) -> dict[str, int]:
    """Stable action counts for acceptance checks and at-a-glance logging."""
    return {
        "creates": len(plan.creates),
        "updates": len(plan.updates),
        "deletes": len(plan.deletes),
    }


def _leg(desired) -> str:
    return f"{desired.kind} {desired.identity}"


def render_plan(plan: ReconcilePlan, *, header: str = "[shadow] reconcile plan") -> str:
    """Render a plan as a human-readable diff — one line per action.

    A no-op plan renders as a single "no changes" line so a quiet sweep is still
    legible in the log.
    """
    counts = plan_counts(plan)
    lines = [
        f"{header}: {counts['creates']} create, {counts['updates']} update, "
        f"{counts['deletes']} delete"
    ]
    if plan.is_noop:
        lines.append("  (no changes)")
        return "\n".join(lines)

    for c in plan.creates:
        lines.append(
            f"  + CREATE {_leg(c.desired)}  [{c.desired.origin} → {c.desired.destination}]"
        )
    for u in plan.updates:
        lines.append(f"  ~ UPDATE {_leg(u.desired)}  (event {u.event_id})")
    for d in plan.deletes:
        lines.append(f"  - DELETE event {d.event_id}  ({d.reason})")
    return "\n".join(lines)
