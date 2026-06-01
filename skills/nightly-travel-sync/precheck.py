#!/usr/bin/env python3
"""Cadence precheck for `tessl__nightly-travel-sync`.

Fires daily via the cadence-registry (`0 6 * * * (TZ=local)`). Gates the
wake on a 3-day filesystem cadence cap anchored on the bundle's terminal
artifact, `/workspace/group/travel-db.json` — the file Step 4 rebuilds
last and the one downstream consumers (`check-travel-bookings`,
`morning-brief`) actually read.

Anchoring on travel-db.json rather than travel-schedule.json is
deliberate: travel-schedule.json (Step 2's output) bumps on every
successful ICS refresh even when a later step fails, which would reset
the cadence while the DB stayed stale. travel-db.json bumps only after
the refresh → build pipeline reaches Step 4, so its mtime is the honest
"the pipeline produced its output" signal — the same semantics the admin
bundle's end-of-run cursor stamp carried before this extract. No
separate cursor file is owned, so the gate adds no self-owned state per
`jbaruch/nanoclaw-admin#318`.

Wake conditions:
  - travel-db.json missing (cold start, or pruned) — wake.
  - travel-db.json mtime older than CADENCE (3 days) — wake.
  - mtime in the future (clock skew / bad write) — wake so the next run
    rewrites it.
  - within cadence — skip silently.

Scheduled-task contract: emits single-line JSON `{"wake_agent": <bool>,
"data": {...}}` on stdout, exit 0 always (per agent-runner contract — a
non-zero exit or invalid stdout is read as wake_agent=false, which would
silently freeze the travel-data refresh). The sole catch-all sits inside
`main()` so the outer-boundary-process-contract carve-out's "outermost
process boundary" precondition holds; it fails OPEN (wake) so a transient
stat error can't freeze the pipeline for days.

stdlib-only per `jbaruch/coding-policy: dependency-management`.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

CADENCE = timedelta(days=3)
DEFAULT_DB_PATH = "/workspace/group/travel-db.json"


def decide(now_utc: datetime, db_path: Path) -> dict:
    if not db_path.exists():
        return {
            "wake_agent": True,
            "data": {"reason": "no_travel_db", "path": str(db_path)},
        }

    mtime = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc)
    age = now_utc - mtime
    age_hours = round(age.total_seconds() / 3600.0, 2)

    if age < timedelta(0):
        return {
            "wake_agent": True,
            "data": {
                "reason": "db_mtime_future",
                "mtime": mtime.isoformat(),
                "age_hours": age_hours,
            },
        }

    if age >= CADENCE:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cadence_elapsed",
                "mtime": mtime.isoformat(),
                "age_hours": age_hours,
                "cadence_hours": CADENCE.total_seconds() / 3600.0,
            },
        }

    return {
        "wake_agent": False,
        "data": {
            "reason": "within_cadence",
            "mtime": mtime.isoformat(),
            "age_hours": age_hours,
            "cadence_hours": CADENCE.total_seconds() / 3600.0,
        },
    }


def main() -> int:
    # outer-boundary-process-contract: the agent-runner reads non-zero
    # exit OR invalid stdout JSON as wake_agent=false, which here would
    # silently freeze the travel-data refresh pipeline. Every unexpected
    # exception flows into a safe-shape JSON payload + exit 0 so the
    # contract stays honest. This handler fails OPEN (wake_agent=true) —
    # a transient stat error must not pin the pipeline closed for days;
    # the bundle is idempotent, so an extra wake is cheap. See
    # `jbaruch/coding-policy: error-handling`. Sole catch-all in the file.
    try:
        db_path = Path(os.environ.get("NIGHTLY_TRAVEL_SYNC_DB", DEFAULT_DB_PATH))
        now = datetime.now(timezone.utc)
        payload = decide(now, db_path)
    except Exception:  # noqa: BLE001 — outer-boundary-process-contract
        traceback.print_exc(file=sys.stderr)
        payload = {"wake_agent": True, "data": {"reason": "precheck_internal_error"}}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
