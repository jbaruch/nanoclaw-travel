---
name: drive-engine
description: "The unified drive-block engine: manages the travel-time / driving blocks on your primary calendar from one place — the drives to your flights, the drives to your in-person meetings, and the drive to a trip you're driving to rather than flying. On a schedule it plans all three, diffs them against the blocks already there, and applies the changes — adding, updating, and removing its own blocks — suppressing drives you can't make (an airport reached by a connection, a home meeting while you're travelling). Use when the user asks about drive blocks, driving time, commute or travel-time blocks on their calendar, or drives to the airport, to a meeting, or to a hotel; when the operator replies to a drive notification to skip a meeting drive they are not making ('skip', 'skip 1', 'skip 2 and 3', 'skip the Massage drive'); when the operator answers whether a trip is a drive or a flight ('drive', 'fly', 'we're driving to that one'); also runs on its own schedule and wakes you only when it added a skippable meeting drive, a drive time materially changed, or it needs to know whether a trip is a drive or a flight."
cadence: "*/30 * * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "reconcile_sweep.py"
---

# Drive Engine

This skill is an action router — pick the step that matches the situation and execute only that step. Do not run other steps; do not parallelize.

The precheck (`reconcile_sweep.py`) plans and applies every `Drive:` block change — airport, meeting, and lodging drives — every ~30 minutes, diffing against the calendar and touching only its own blocks. It leaves legacy drive-planner / flight-assist blocks alone (you clean those up). Its contract — inputs, apply counts, the fail-closed no-wake payload on error, and the wake gating — lives in `reconcile_sweep.py` (module docstring, `build_sweep_payload`) and `calendar_apply.apply_plan`. Do not restate its logic here.

## Step 1 — Send the sweep's notice

Run this after a cadence sweep that woke you (`wake_agent: true`). The precheck has already rendered the operator notice deterministically (`render_notification` in `reconcile_sweep.py` — the one-line templates and enumeration live there). Send `data.message` verbatim via `mcp__nanoclaw__send_message`, then finish.

Relay the string as-is: do not rewrite, summarize, add to it, or reference anything from a prior wake — each notice stands alone, composed only from this sweep's payload. If `data.message` is absent or empty, proceed silently and finish. Finish here.

## Step 2 — Skip a meeting drive the operator declined

Run this when the operator replies to a drive notification to skip one — "skip", "skip 1", "skip 2 and 3", "skip the Massage drive". Map each local index to the meeting NAME from the message you sent (index 1 = the first meeting listed); a bare "skip" refers to the single meeting just offered. Never surface an internal id — the operator only ever named the drive by its position or name. For each named meeting, invoke:

```bash
python3 /home/node/.claude/skills/tessl__drive-engine/skip_drive.py '{"summary": "<meeting name>"}'
```

The script deletes that meeting's drive blocks and records a skip so no future sweep recreates them. It always prints a JSON result on stdout; read it (do not treat a non-zero exit as "no result"):
- `{"skipped": true, "meeting": ...}` (exit 0) — confirm what you skipped.
- `{"skipped": false, "unmatched": ...}` (exit 0) — the name wasn't found; say so.
- `{"skipped": false, "ambiguous": ..., "candidates": [...]}` (exit 0) — several same-named meetings; ask the operator which `when` they mean, then re-invoke.
- `{"skipped": false, "error": ...}` (exit 1) — an operational failure (an unauthenticated gateway, transport error); tell the operator the skip couldn't be recorded and to retry. Exit 2 is a usage/JSON error in how it was invoked — fix the call.

Reply in one message. Finish here.

## Step 3 — Record a drive-or-fly answer

Run this when the operator answers a drive-or-fly question the sweep asked about a trip with no flight booked — "drive", "fly", "we're driving", "I'll fly that one". Map the answer to the trip NAME from the message you sent; a bare "drive" or "fly" refers to the single trip just asked about. Never surface an internal key — the operator only ever saw the trip name. Invoke:

```bash
python3 /home/node/.claude/skills/tessl__drive-engine/answer_drive_or_fly.py '{"trip": "<trip name>", "answer": "drive"}'
```

`answer` is `drive` or `fly`, nothing else. The script records the answer so it outranks the drive time from then on and the question is not repeated. It always prints a JSON result on stdout; read it (do not treat a non-zero exit as "no result"):
- `{"recorded": true, "trip": ..., "answer": ...}` (exit 0) — confirm what you recorded. On `drive`, the next sweep adds the drives; on `fly`, the booking check reports the missing flight.
- `{"recorded": false, "unmatched": ...}` (exit 0) — the trip name wasn't found; say so.
- `{"recorded": false, "ambiguous": ..., "candidates": [...]}` (exit 0) — several same-named trips; ask the operator which `when` they mean, then re-invoke.
- `{"recorded": false, "error": ...}` (exit 1) — an operational failure; tell the operator the answer couldn't be recorded and to retry. Exit 2 is a usage error in how it was invoked — fix the call.

Reply in one message. Finish here.

## Step 4 — Flag a block that looks wrong

Run this when a block looks like an engine bug — a drive for a meeting you are travelling away from, a wrong-timezone block, a missing drive for a real trip. Send one message via `mcp__nanoclaw__send_message` naming the block's summary and leg identity (e.g. `meeting_outbound mtg123` or `airport_departure BNA-STN-...`) and what looks wrong, so it can be fixed in code. Never edit the calendar by hand to compensate. Finish here.
