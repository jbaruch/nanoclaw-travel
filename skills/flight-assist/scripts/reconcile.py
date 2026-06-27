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
     "executed": N, "archived": N, "failed": [...], "airport_drive": {...}}

Some keys vary by `status`: `byair_calendar_id` and `archived` are present only
when a cycle actually ran (`ok` / `no_flights`) and are omitted on `no_calendar`.
`airport_drive` is present on every NON-error summary (`ok` / `no_calendar` /
`no_flights`) — it runs independently, see below — but is absent on the early
`{"status": "error", "error": "credentials" | "state"}` setup-failure exits,
which return before it runs (no Composio client / unreadable state).

`status` is `ok` (a cycle ran), `no_calendar` (no flight calendar resolved
from config — reconciliation disabled, like maps with no key), or
`no_flights` (nothing tracked to reconcile). Per-op failures are collected in
`failed`, not fatal — a failed Composio call defers that op to the next
cycle. Exit 0 when a cycle ran (even with collected per-op failures); exit 1
on a setup failure that makes the run meaningless (missing credentials,
unreadable state).

`airport_drive` carries the parallel reconcile of the airport drive blocks
(#90) on the primary calendar — independent of the byAir flight calendar, so it
runs even when `status` is `no_calendar`. It stays a dormant idle summary when
routing inputs are absent (no Maps key / byAir URL / tracked flights), and a
transient byAir/Maps/Composio failure during it is logged and recorded as
`{"status": "error"}` without failing the rest of the cycle.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BUNDLE_DIR))

from airport_drive_reconcile import run_airport_drive_pass  # noqa: E402
from byair_client import ByAirError  # noqa: E402
from calendar_reconcile import run_reconcile  # noqa: E402
from composio_client import ComposioClient, ComposioError  # noqa: E402
from maps_client import MapsError  # noqa: E402
from state import StateError  # noqa: E402


def main() -> int:
    # Scope the credential catch to construction ONLY. run_reconcile raises
    # ValueError subclasses of its own (PlanError, DispositionError,
    # NormalizeError) and from state writes; folding those into this handler
    # would mislabel a data/validation bug as a credentials failure and point
    # the operator at the wrong fix. Keep the two failure surfaces separate.
    try:
        client = ComposioClient.from_env()
    except ValueError as exc:
        # Missing / empty Composio credentials — a setup failure. Actionable
        # message to stderr, safe-shape JSON to stdout, non-zero exit.
        print(f"flight-assist reconcile: {exc}", file=sys.stderr)
        print(json.dumps({"status": "error", "error": "credentials"}, separators=(",", ":")))
        return 1

    now = datetime.now(timezone.utc)
    try:
        summary = run_reconcile(client, now=now)
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

    # Airport drive blocks (#90) reconcile on the PRIMARY calendar, independent
    # of the byAir flight calendar above — run them even when that returned
    # no_calendar. A transient byAir / Maps / Composio failure here is logged and
    # recorded without failing the rest of the cycle; StateError still propagates
    # to the outer boundary as an unexpected error.
    try:
        summary["airport_drive"] = run_airport_drive_pass(client, now=now)
    except (ComposioError, ByAirError, MapsError, urllib.error.URLError) as exc:
        print(f"flight-assist reconcile: airport-drive pass failed: {exc}", file=sys.stderr)
        summary["airport_drive"] = {"status": "error"}

    print(json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
