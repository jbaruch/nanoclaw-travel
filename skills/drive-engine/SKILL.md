---
name: drive-engine
description: "Previews airport drive-block planning against your calendar in read-only mode. On a schedule it works out which airport drive blocks your flights need, compares that to the drive blocks already on your primary calendar, and logs the differences — which blocks it would add, move, delete, or replace — without changing the calendar. Use to validate the new drive planner against real data before it is allowed to write. It logs and does not notify you on a normal run."
cadence: "*/30 * * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "shadow_precheck.py"
---

# Drive Engine (Preview Mode)

Process steps in order. Do not skip ahead.

This skill previews the new drive planner against real data. Every ~30 minutes its precheck (`shadow_precheck.py`) reads your flights and your primary calendar, works out the plan of drive blocks to add / move / delete / replace, and writes that plan to the run log. It never changes the calendar, and its precheck payload always reports `wake_agent: false`, so on a normal run you are not notified and there is nothing for the agent to do. The point is the logged plan: you read it to confirm the planner is right before it is allowed to write.

## Step 1 — Confirm the preview run

The precheck already logged the plan (to stderr) and returned a no-wake payload (to stdout). You are not notified on a normal run, so there is nothing to act on. Proceed silently and finish here.

The precheck's contract — its inputs, the add/move/delete/replace plan shape, the always-`false` `wake_agent`, and the fail-closed no-wake payload on error — lives in `shadow_precheck.py` (module docstring and `build_shadow_result`). Do not restate its logic here.

## Step 2 — If the logged plan looks wrong, report it

Only when a person is reviewing these preview logs. A wrong plan is a bug in the planner, not a calendar action. Examples of wrong: a `DELETE` line for a block that should stay (e.g. a real `→ BNA` departure drive), a missing `DELETE` for a duplicate-storm block, or a `CREATE`/`CONVERT` whose origin or destination is the wrong place. Copy the offending plan line and its leg identity (e.g. `airport_departure BNA-STN-20260712T0600Z`) from the log and report it for a code fix. Never edit the calendar by hand to compensate. Finish here.
