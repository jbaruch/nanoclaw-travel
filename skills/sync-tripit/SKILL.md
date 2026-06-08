---
name: sync-tripit
description: "Adaptive scheduler for the TripIt/byAir refresh of active-flights.json. Precheck-gated to keep byAir polling responsive on flight days and idle between travel windows. Use when active-flights.json isn't updating, byAir polling cadence isn't matching flight density, troubleshooting flight tracking / flight notifications / flight status updates / travel schedule refresh, or setting up flight-assist on a new install. The gate predicate and threshold constants live in precheck.py."
cadence: "*/5 * * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "precheck.py"
user-invocable: false
---

# sync-tripit (adaptive scheduler)

Process steps in order. Do not skip ahead.

**Silence rule:** produces NO user-visible output. The precheck delegates wake-payload composition to `flight-assist/sync_tripit.py`, which emits `wake_agent: true` only on tracked-flight added/removed events.

## Hard rules

- **No byAir call without the gate passing.** `precheck.py:_should_sync_now()` is the sole authority on whether a 5-min fire becomes a byAir round-trip.
- **No state writes here.** All state mutations live in `flight-assist/sync_tripit.py` + `flight-assist/state.py`.

## Step 1 — Diagnose a failed precheck run

This step fires only when the precheck itself failed to execute. Gate-passed runs forward `sync_tripit.py`'s output directly; gate-failed runs emit silent `wake_agent: false`. Reaching this skill body means the outer-boundary handler caught an exception and emitted `{"wake_agent": false, "data": {"reason": "precheck_internal_error"}}`.

Inspect the agent-runner host log for the Python traceback the script wrote to stderr, then match against:

**Corrupt state file** — `flight-assist/state.py` raises `StateError` on JSON corruption or schema-version mismatch. Inspect and remove:
```bash
python3 -m json.tool /workspace/state/flight-assist/active-flights.json
ls -la /workspace/state/flight-assist/flight-*.json
# remove any file that fails parse — next sync repopulates from byAir
rm /workspace/state/flight-assist/<bad-file>.json
```

**Missing flight-assist mount** — the precheck imports from the co-shipped `flight-assist` skill. If only `sync-tripit` is installed, the precheck raises `FileNotFoundError` at import. Verify:
```bash
ls /home/node/.claude/skills/tessl__flight-assist/sync_tripit.py
```
If absent, both skills must be reinstalled from the same tile (`jbaruch/nanoclaw-flight-assist`).

**byAir subprocess timeout** — the precheck enforces a budget on `sync_tripit.py` (see `precheck.py`, `_SYNC_SUBPROCESS_TIMEOUT` constant). Persistent timeouts mean byAir is degraded; the next cadence cycle retries. Check the diagnostic log for the `sync_subprocess_timeout` reason:
```bash
grep sync_subprocess_timeout /workspace/state/flight-assist/precheck.log 2>/dev/null | tail -5
```

**Filesystem permissions** — read access to `/workspace/state/flight-assist/`. Verify the mount:
```bash
ls -la /workspace/state/flight-assist/
```

Finish here. **Verify the fix** by waiting for the next 5-min cadence fire and reading the host log for either a clean `wake_agent: false` (no `precheck_internal_error` payload) or a successful `sync_tripit` delegation. Do not retry inline; the next fire retries naturally.

## References

- [../flight-assist/sync_tripit.py](../flight-assist/sync_tripit.py) — the byAir → state sync this skill schedules
- [../flight-assist/state.py](../flight-assist/state.py) — state readers + `state_dir()`
- [../flight-assist/state-schema.md](../flight-assist/state-schema.md) — `active-flights.json` and per-flight state shapes
