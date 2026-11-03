---
name: sync-tripit
description: "Adaptive scheduler for the TripIt/byAir refresh of active-flights.json. Precheck-gated to keep byAir polling responsive on flight days and idle between travel windows. Its precheck sends the sync's notices (tracked-flight changes, sync outage and recovery) through the host itself. Action: diagnose a failed precheck run. Use when active-flights.json isn't updating, byAir polling cadence isn't matching flight density, troubleshooting flight tracking / flight notifications / flight status updates / travel schedule refresh, or setting up flight-assist on a new install. The gate predicate and threshold constants live in skills/sync-tripit/precheck.py."
cadence: "*/5 * * * *"
# agentModel renewal: no scanner tracks this pin. Re-check it on each new Claude model release and bump it in its own PR.
agentModel: "claude-haiku-5-5"
script: "precheck.py"
precheck_timeout_ms: 120000
user-invocable: false
---

# sync-tripit (adaptive scheduler)

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Available actions:
- Diagnose a failed precheck run (operator request while troubleshooting a sync that isn't updating; the run log shows `precheck_internal_error`)

**Silence rule:** produces NO user-visible output of its own. `skills/flight-assist/sync_tripit.py` reports tracked-flight added/removed events and the sync outage events (`byair_unreachable`, `byair_recovered`, `sync_internal_error`, `sync_internal_recovered`). The precheck renders each with `skills/flight-assist/notices.py` and sends it through the host itself (#370); it never wakes the model. A send the host does not confirm makes the run exit non-zero, so it shows as an error in the run log.

## Hard rules

- **No byAir call without the gate passing.** `skills/sync-tripit/precheck.py:_should_sync_now()` is the sole authority on whether a 5-min fire becomes a byAir round-trip.
- **No state writes here.** All state mutations live in `skills/flight-assist/sync_tripit.py` + `skills/flight-assist/state.py`.

## Step 1 — Diagnose a failed precheck run

This step fires on an operator request while troubleshooting, never on a wake. A precheck that failed to execute emits `{"wake_agent": false, "data": {"reason": "precheck_internal_error"}}` and wakes nobody; the operator finds that reason in the run log. A sync that fails repeatedly sends the owner a `sync_internal_error` notice through the host.

Inspect the agent-runner host log for the Python traceback the script wrote to stderr, then match against:

**Corrupt state file** — `skills/flight-assist/state.py` raises `StateError` on JSON corruption or schema-version mismatch. Inspect and remove:
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
If absent, both skills must be reinstalled from the same plugin (`jbaruch/nanoclaw-travel`).

**byAir subprocess timeout** — the precheck enforces a budget on `skills/flight-assist/sync_tripit.py` (see `skills/sync-tripit/precheck.py`, `_SYNC_SUBPROCESS_TIMEOUT` constant). Persistent timeouts mean byAir is degraded; the next cadence cycle retries. Check the diagnostic log for the `sync_subprocess_timeout` reason:
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
