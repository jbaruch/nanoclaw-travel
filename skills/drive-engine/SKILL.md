---
name: drive-engine
description: "The unified drive-block engine: manages the travel-time / driving blocks on your primary calendar from one place — the drives to your flights and the drives to your in-person meetings. On a schedule it plans both, diffs them against the blocks already there, and applies the changes — adding, updating, and removing its own blocks — suppressing drives you can't make (an airport reached by a connection, a home meeting while you're travelling). Use when the user asks about drive blocks, driving time, commute or travel-time blocks on their calendar, or drives to the airport or to a meeting; also runs on its own schedule and wakes you only when it changed something."
cadence: "*/30 * * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "reconcile_sweep.py"
---

# Drive Engine

Process steps in order. Do not skip ahead.

The precheck (`reconcile_sweep.py`) plans and applies every `Drive:` block change — airport and meeting drives — every ~30 minutes, diffing against the calendar and touching only its own blocks. It leaves legacy drive-planner / flight-assist blocks alone (you clean those up), and the two old engines are retired so they no longer write.

## Step 1 — Report what changed, then finish

The precheck already applied the plan and returned a payload with `data.applied` (created / updated / deleted / converted counts). If it woke you (`wake_agent: true`), send the operator one brief message via `mcp__nanoclaw__send_message` naming what changed — e.g. "added 2 drive blocks, removed 1 stale one" — then **finish here**. If nothing changed, proceed silently and **finish here**. Either way this is the end of a normal run; do not continue to Step 2.

The precheck's contract — its inputs, the apply counts, the always-fail-closed no-wake payload on error — lives in `reconcile_sweep.py` (module docstring, `build_plan`, and `calendar_apply.apply_plan`). Do not restate its logic here.

## Step 2 — If a block looks wrong, tell the operator

A wrong block is an engine bug, not a calendar action to take by hand. Examples: a drive created for a meeting you're travelling away from, a block in the wrong timezone, or a missing drive for a real trip. Send the operator one message via `mcp__nanoclaw__send_message` naming the block's summary and leg identity (e.g. `meeting_outbound mtg123` or `airport_departure BNA-STN-...`) and what looks wrong, so it can be fixed in code. Never edit the calendar by hand to compensate. Finish here.
