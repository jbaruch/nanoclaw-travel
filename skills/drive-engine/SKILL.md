---
name: drive-engine
description: "The unified drive-block engine: manages the travel-time / driving blocks on your primary calendar from one place — the drives to your flights and the drives to your in-person meetings. On a schedule it plans both, diffs them against the blocks already there, and applies the changes — adding, updating, and removing its own blocks — suppressing drives you can't make (an airport reached by a connection, a home meeting while you're travelling). Use when the user asks about drive blocks, driving time, commute or travel-time blocks on their calendar, or drives to the airport or to a meeting; when the operator replies to a drive notification to skip a meeting drive they are not making ('skip', 'skip 1', 'skip 2 and 3', 'skip the Massage drive'); also runs on its own schedule and wakes you only when it added a skippable meeting drive or a drive time materially changed."
cadence: "*/30 * * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "reconcile_sweep.py"
---

# Drive Engine

This skill is an action router — pick the step that matches the situation and execute only that step. Do not run other steps; do not parallelize.

The precheck (`reconcile_sweep.py`) plans and applies every `Drive:` block change — airport and meeting drives — every ~30 minutes, diffing against the calendar and touching only its own blocks. It leaves legacy drive-planner / flight-assist blocks alone (you clean those up). Its contract — inputs, apply counts, the fail-closed no-wake payload on error, and the wake gating — lives in `reconcile_sweep.py` (module docstring, `build_sweep_payload`) and `calendar_apply.apply_plan`. Do not restate its logic here.

## Step 1 — Report what the sweep changed

Run this after a cadence sweep that woke you (`wake_agent: true`). The payload's `data` carries `material_updates` and `added_meeting_drives`; compose ONE message via `mcp__nanoclaw__send_message` from them, then finish. The sweep already gated the noise — it wakes ONLY for these two, so if both are empty, proceed silently and finish.

- **Material drive-time changes** (`material_updates` — a `{meeting, minutes, direction, when}` list): one line each, e.g. "Traffic: leave {minutes} min {direction} for your {meeting} at {when}" — `direction` is `sooner` (drive got longer) or `later` (shorter).
- **New meeting drives** (`added_meeting_drives` — a `{meeting, when}` list the operator may skip): with one, "Added a drive for {meeting} at {when} — reply 'skip' if you're not driving to it." With several, enumerate them 1..N ("1. {meeting} at {when}") and tell the operator to reply "skip 1", "skip 2", or e.g. "skip 1 and 2" for any they are not driving.

Never mention removed blocks, airport drives, or routine (sub-threshold) re-times — those applied silently by design. Finish here.

## Step 2 — Skip a meeting drive the operator declined

Run this when the operator replies to a drive notification to skip one — "skip", "skip 1", "skip 2 and 3", "skip the Massage drive". Map each local index to the meeting NAME from the message you sent (index 1 = the first meeting listed); a bare "skip" refers to the single meeting just offered. Never surface an internal id — the operator only ever named the drive by its position or name. For each named meeting, invoke:

```bash
python3 /home/node/.claude/skills/tessl__drive-engine/skip_drive.py '{"summary": "<meeting name>"}'
```

The script deletes that meeting's drive blocks and records a skip so no future sweep recreates them. Its result is `{"skipped": true, "meeting": ...}`, `{"skipped": false, "unmatched": ...}` (name not found — say so), or `{"skipped": false, "ambiguous": ..., "candidates": [...]}` (several same-named meetings — ask the operator which `when` they mean, then re-invoke). Confirm what you skipped in one message. Finish here.

## Step 3 — Flag a block that looks wrong

Run this when a block looks like an engine bug — a drive for a meeting you are travelling away from, a wrong-timezone block, a missing drive for a real trip. Send one message via `mcp__nanoclaw__send_message` naming the block's summary and leg identity (e.g. `meeting_outbound mtg123` or `airport_departure BNA-STN-...`) and what looks wrong, so it can be fixed in code. Never edit the calendar by hand to compensate. Finish here.
