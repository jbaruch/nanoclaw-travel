#!/usr/bin/env python3
"""Run one calendar reconciliation cycle and print a JSON summary.

Invoked by SKILL.md on the wake cycle (the byAir change events that already
wake the agent — delay / gate_change / schedule_slip / cancelled / diverted,
plus the boarding window). Deterministic glue, no LLM: it resolves the
calendar IDs, fetches the current calendar state via Composio, runs the pure
planner, executes the resulting ops, and writes the owned event IDs back into
each flight's `calendar_events` ledger. See `calendar_reconcile.py`.

Usage:
    reconcile.py

Output: single-line JSON summary on stdout —
    {"status": "...", "byair_calendar_id": "...", "planned": N,
     "executed": N, "failed": [...]}

`status` is `ok` (a cycle ran), `no_calendar` (no flight calendar resolved
from config — reconciliation disabled, like maps with no key), or
`no_flights` (nothing tracked to reconcile). Per-op failures are collected in
`failed`, not fatal — a failed Composio call defers that op to the next
cycle. Exit 0 when a cycle ran (even with collected per-op failures); exit 1
on a setup failure that makes the run meaningless (missing credentials,
unreadable state).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BUNDLE_DIR))

from calendar_reconcile import run_reconcile  # noqa: E402
from composio_client import ComposioClient  # noqa: E402
from state import StateError  # noqa: E402


def main() -> int:
    try:
        client = ComposioClient.from_env()
        summary = run_reconcile(client, now=datetime.now(timezone.utc))
    except ValueError as exc:
        # Missing / empty Composio credentials (ComposioClient.from_env) — a
        # setup failure, not a per-op failure. Actionable message to stderr,
        # safe-shape JSON to stdout, non-zero exit so the agent can report it.
        print(f"flight-assist reconcile: {exc}", file=sys.stderr)
        print(json.dumps({"status": "error", "error": "credentials"}, separators=(",", ":")))
        return 1
    except StateError as exc:
        # On-disk state is corrupt / unreadable — reconciliation cannot run
        # this cycle. Surface it; do not pretend a clean no-op.
        print(f"flight-assist reconcile: {exc}", file=sys.stderr)
        print(json.dumps({"status": "error", "error": "state"}, separators=(",", ":")))
        return 1
    # Any genuinely unexpected exception propagates: a non-zero exit with a
    # traceback on stderr is visible failure, per `coding-policy:
    # error-handling` (let unexpected exceptions propagate).
    print(json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
