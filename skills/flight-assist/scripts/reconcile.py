#!/usr/bin/env python3
"""Run one calendar reconciliation cycle and print a JSON summary.

Invoked by SKILL.md on the wake cycle (the byAir change events that already
wake the agent — delay / gate_change / schedule_slip / cancelled / diverted,
plus the boarding window). Deterministic glue, no LLM: it resolves the
calendar IDs, fetches the current calendar state from Google Calendar, runs the pure
planner, executes the resulting ops, and writes the owned event IDs back into
each flight's `calendar_events` ledger. See `calendar_reconcile.py`.

Usage:
    reconcile.py

Output: single-line JSON summary on stdout —
    {"status": "...", "byair_calendar_id": "...", "planned": N,
     "executed": N, "archived": N, "failed": [...], "airport_drive": {...}}

Some keys vary by `status`: `byair_calendar_id` and `archived` are present only
when a cycle actually ran (`ok` / `no_flights`) and are omitted on `no_calendar`.
`airport_drive` is present on every NON-error summary (`ok` / `no_calendar` /
`no_flights`) — it runs independently, see below — but is absent on the
`{"status": "error", "error": "gateway" | "tier" | "state"}` failure exits,
which return before it runs.

`status` is `ok` (a cycle ran), `no_calendar` (no flight calendar resolved
from config — reconciliation disabled, like maps with no key), or
`no_flights` (nothing tracked to reconcile). Per-op failures are collected in
`failed`, not fatal — a failed Calendar call defers that op to the next
cycle. Exit 0 when a cycle ran (even with collected per-op failures); exit 1
on a failure that makes the whole run meaningless: `gateway` (the OneCLI
gateway is not authenticating the requests), `tier` (this agent's tier is
gated from Google by design), or `state` (unreadable on-disk state).

`airport_drive` is now a dormant `{"status": "retired", "engine": "drive-engine"}`
marker: airport drive blocks are owned by the unified drive-engine (#156), which
plans and applies them from its own precheck. flight-assist no longer reconciles
them here; the key is retained only so the summary shape stays stable.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BUNDLE_DIR))

from calendar_reconcile import run_reconcile  # noqa: E402
from google_calendar_client import (  # noqa: E402
    GatewayNotInjecting,
    GoogleCalendarClient,
    TierAccessRestricted,
)
from state import StateError  # noqa: E402


def main() -> int:
    # Construction cannot fail: the client holds no credential, so there is no
    # missing-key setup failure to catch here any more (#638). The two config
    # failures it replaced are discovered on the first CALL and handled below.
    client = GoogleCalendarClient()

    now = datetime.now(timezone.utc)
    try:
        summary = run_reconcile(client, now=now)
    except GatewayNotInjecting as exc:
        # The OneCLI gateway is not authenticating our requests — every op this
        # cycle would fail the same way, and no retry fixes it. Actionable
        # message to stderr, safe-shape JSON to stdout, non-zero exit.
        print(f"flight-assist reconcile: unauthenticated — {exc}", file=sys.stderr)
        print(json.dumps({"status": "error", "error": "gateway"}, separators=(",", ":")))
        return 1
    except TierAccessRestricted as exc:
        # This agent's tier is gated from Google by design, not broken.
        print(f"flight-assist reconcile: unavailable at this tier — {exc}", file=sys.stderr)
        print(json.dumps({"status": "error", "error": "tier"}, separators=(",", ":")))
        return 1
    except StateError as exc:
        # On-disk state is corrupt / unreadable — reconciliation cannot run
        # this cycle. Surface it; do not pretend a clean no-op.
        print(f"flight-assist reconcile: {exc}", file=sys.stderr)
        print(json.dumps({"status": "error", "error": "state"}, separators=(",", ":")))
        return 1
    # Any other exception (incl. a ValueError from the reconcile itself)
    # propagates: a non-zero exit with a traceback on stderr is visible
    # failure under its real cause, per `coding-policy: error-handling`
    # (catch specific types; let unexpected exceptions propagate).

    # Airport drive blocks are now owned by the unified drive-engine (#156).
    # flight-assist no longer reconciles them here (the two-engine patchwork is
    # retired); the drive-engine's own precheck plans and applies airport drives.
    # A dormant marker keeps the summary shape stable for the SKILL's reader.
    summary["airport_drive"] = {"status": "retired", "engine": "drive-engine"}

    print(json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
