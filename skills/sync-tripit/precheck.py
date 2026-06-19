#!/usr/bin/env python3
"""Adaptive precheck for sync_tripit (TripIt/byAir → active-flights.json refresh).

Fires every 5 minutes via the cadence-registry. Most fires are no-ops:
the precheck reads `/workspace/state/flight-assist/active-flights.json`
and per-flight state files via the non-owner snapshot API in
`state.py`, decides whether the byAir round-trip is warranted, and
emits `wake_agent: false` when it isn't. The byAir call fires only
when:

  - Any tracked flight has scheduled_dep_time within the next 24 hours
    (day-of-travel polling for delays / gate changes / cancellations).
  - `active-flights.json` mtime is older than 6 hours (catches newly-
    booked trips landing in TripIt between travel windows).
  - No state has been written yet (cold start).

When the gate passes, the precheck delegates to flight-assist's
`sync_tripit.py` via subprocess and forwards its stdout — sync_tripit
already emits the same `{wake_agent, data}` contract this script
needs, so the wake-payload composition lives in one place.

Scheduled-task contract: emits single-line JSON `{"wake_agent": <bool>,
"data": {...}}` on stdout. Exit 0 always (per agent-runner contract —
non-zero exit silently disables the wake, per `coding-policy:
error-handling` outer-boundary-process-contract). The sole catch-all
sits inside `main()` so the carve-out's "outermost process boundary"
precondition holds; all bootstrap (path resolution + state import)
happens inside that same try block.

stdlib-only per `coding-policy: dependency-management`.

References:
  - skills/flight-assist/sync_tripit.py — the underlying byAir sync
  - skills/flight-assist/state.py — read_active_flights_snapshot +
    read_flight_state_snapshot (non-owner reader API per
    coding-policy: stateful-artifacts)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

# Locations the co-shipped flight-assist skill may live at. Runtime is
# the container mount; dev is the sibling clone path the test suite
# resolves under pytest. Both checked inside main()'s try block — no
# module-level filesystem access, so a missing co-deployment surfaces
# through the outer-boundary handler rather than via an import-time
# crash.
_FLIGHT_ASSIST_RUNTIME = Path("/home/node/.claude/skills/tessl__flight-assist")
_FLIGHT_ASSIST_DEV = Path(__file__).resolve().parent.parent / "flight-assist"

# Gate thresholds. Imminent-flight window matches the user-stated
# requirement ("only that frequent when there are flights in the next
# 24 hours"). Stale-state threshold catches new bookings landing in
# TripIt between travel windows — 6h is well under the typical "I
# booked something" → "I want it to show up" gap, and well over the
# 5-min cadence so the gate doesn't pin-fire every cycle.
_IMMINENT_FLIGHT_WINDOW = timedelta(hours=24)
_STALE_STATE_THRESHOLD = timedelta(hours=6)

# Subprocess timeout for the sync_tripit.py delegation. byAir has a per-
# call 30s timeout in its own client; one list_trips + N per-flight
# state writes is bounded comfortably below 60s.
_SYNC_SUBPROCESS_TIMEOUT = 60.0


def _load_flight_assist() -> tuple[ModuleType, Path]:
    """Resolve and import the co-shipped flight-assist skill.

    Returns `(state_module, sync_tripit_path)`. Raises `FileNotFoundError`
    when neither the runtime mount nor the dev-clone location holds the
    skill — main()'s outer-boundary handler converts that into the
    safe-shape wake payload. Idempotent: re-importing `state` is a
    cheap dict lookup in `sys.modules` after the first call, and
    repeated `sys.path.insert(0, ...)` of the same prefix is a no-op
    for module resolution (Python deduplicates by path during import).
    """
    if _FLIGHT_ASSIST_RUNTIME.is_dir():
        flight_assist_dir = _FLIGHT_ASSIST_RUNTIME
    elif _FLIGHT_ASSIST_DEV.is_dir():
        flight_assist_dir = _FLIGHT_ASSIST_DEV
    else:
        raise FileNotFoundError(
            "sync-tripit precheck: cannot locate the co-shipped flight-assist skill at "
            f"{_FLIGHT_ASSIST_RUNTIME} (runtime) or {_FLIGHT_ASSIST_DEV} (dev). Both skills "
            "ship from the same tile (jbaruch/nanoclaw-travel); if one is missing the "
            "other can't function."
        )
    sys.path.insert(0, str(flight_assist_dir))
    import state as state_module

    return state_module, flight_assist_dir / "sync_tripit.py"


def _should_sync_now(state_module: ModuleType, *, now: datetime) -> tuple[bool, str]:
    """Decide whether this 5-min fire should hit byAir.

    Returns `(should, reason)` where `reason` is an opaque diagnostic
    string suitable for inclusion in the wake-payload's `data.reason`
    field. Reasons are stable enough for log-grep but not parsed by
    consumers.

    Uses the non-owner snapshot readers per `coding-policy:
    stateful-artifacts` — schema_version mismatch is treated as "no
    usable prior state" and never triggers an owner-side rewrite.
    """
    # Cold start — no state file yet, OR an old-schema file the owner
    # hasn't yet migrated. Both shapes resolve to "no usable prior
    # state" via the snapshot reader; we sync so the index gets
    # populated (and the owner sync_tripit.py call will rewrite it at
    # the latest schema).
    try:
        flight_ids = state_module.read_active_flights_snapshot()
    except state_module.StateError:
        # Corrupt state file — propagate to the outer-boundary handler;
        # this is a real fault that needs operator attention, not a
        # gate-skip case.
        raise

    if not flight_ids:
        # State file missing, empty, or old-schema. Use the mtime check
        # to decide whether to poll: a present-but-empty file with
        # recent mtime means the last sync confirmed zero tracked
        # flights; an absent or stale file means we should sync now.
        return _stale_state_check(state_module, now=now)

    # Imminent-flight check — any tracked flight's scheduled_dep_time
    # within the next 24 hours triggers a sync. Iteration is bounded by
    # the number of tracked flights (typically <10).
    threshold = now + _IMMINENT_FLIGHT_WINDOW
    for fid in flight_ids:
        flight_state = state_module.read_flight_state_snapshot(fid)
        if flight_state is None:
            # No usable per-flight state (file missing or old schema).
            # Fall through; the owner's next sync will rewrite it.
            continue
        dep_str = flight_state.get("scheduled_dep_time")
        if not dep_str:
            continue
        try:
            dep_time = datetime.fromisoformat(dep_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            # Malformed scheduled_dep_time — skip this flight in the
            # gate (don't crash). The owner skill's writer validates
            # the field; a malformed value here means a writer regressed
            # and should be filed as a bug, not handled by the gate.
            continue
        if now <= dep_time <= threshold:
            return True, f"imminent_flight_{fid}"

    # No imminent flights — fall through to the stale-state check to
    # catch newly-booked trips.
    return _stale_state_check(state_module, now=now)


def _stale_state_check(state_module: ModuleType, *, now: datetime) -> tuple[bool, str]:
    """Return (should_sync, reason) based on active-flights.json mtime."""
    active_path = state_module.state_dir() / state_module.ACTIVE_FLIGHTS_FILE
    if not active_path.exists():
        return True, "cold_start_no_state_file"
    mtime = datetime.fromtimestamp(active_path.stat().st_mtime, tz=timezone.utc)
    age = now - mtime
    if age > _STALE_STATE_THRESHOLD:
        return True, f"stale_state_age_{int(age.total_seconds() // 60)}min"
    return False, "no_imminent_flights_recent_sync"


def _emit(payload: dict) -> None:
    """Write the single-line JSON wake-payload to stdout."""
    print(json.dumps(payload, separators=(",", ":")))


def main() -> int:
    # outer-boundary-process-contract: the agent-runner reads non-zero
    # exit OR invalid stdout JSON as wake_agent=false, which here
    # silently disables the entire flight-assist polling pipeline.
    # Every unexpected exception — including bootstrap failures from
    # `_load_flight_assist` — flows into the safe-shape JSON path and
    # exit 0 so the contract stays honest. See coding-policy:
    # error-handling. This is the sole catch-all in the file.
    try:
        state_module, sync_tripit_path = _load_flight_assist()
        now = datetime.now(timezone.utc)
        should_sync, reason = _should_sync_now(state_module, now=now)

        if not should_sync:
            _emit({"wake_agent": False, "data": {"reason": reason}})
            return 0

        # Gate passed — delegate to flight-assist's sync_tripit.py.
        # That script emits the same {wake_agent, data} contract we
        # need, so forward its stdout verbatim. Inherit env (including
        # FLIGHT_ASSIST_STATE_DIR + BYAIR_MCP_URL).
        result = subprocess.run(
            [sys.executable, str(sync_tripit_path)],
            capture_output=True,
            text=True,
            timeout=_SYNC_SUBPROCESS_TIMEOUT,
            env=os.environ.copy(),
            check=False,
        )
        # Forward sync_tripit.py's diagnostic stderr verbatim (preserves
        # tracebacks from its outer-boundary-process-contract handler).
        if result.stderr:
            sys.stderr.write(result.stderr)
        # Forward stdout — sync_tripit.py is contracted to emit the
        # wake-payload JSON on stdout. If it didn't (subprocess crash,
        # empty output), emit a safe-shape payload so the cadence-
        # registry doesn't silently disable the next wake decision.
        if result.stdout.strip():
            sys.stdout.write(result.stdout)
        else:
            _emit(
                {"wake_agent": False, "data": {"reason": "sync_no_output", "gate_reason": reason}}
            )
        return 0
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"sync-tripit precheck: sync_tripit.py exceeded {_SYNC_SUBPROCESS_TIMEOUT}s budget\n"
        )
        _emit({"wake_agent": False, "data": {"reason": "sync_subprocess_timeout"}})
        return 0
    except Exception:  # noqa: BLE001 — outer-boundary-process-contract
        traceback.print_exc(file=sys.stderr)
        _emit({"wake_agent": False, "data": {"reason": "precheck_internal_error"}})
        return 0


if __name__ == "__main__":
    sys.exit(main())
