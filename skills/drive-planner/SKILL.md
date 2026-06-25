---
name: drive-planner
description: "Ground-transit drive planner for in-person meetings. On a ~2h precheck sweep it creates a traffic-aware Free drive block (home → venue → home) for each in-person meeting that lacks one and tells the user, who can reply to skip; the recheck poll then watches each block for traffic growth. Use on a drive-planner sweep wake event, or when the user replies to skip a drive block. Triggers - 'drive block', 'plan my drive', 'skip drive <id>', 'don't drive to that meeting', 'remove drive block', 'drive to my meeting', 'leave-by for a meeting'."
cadence: "0 */2 * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "precheck.py"
---

# Drive Planner

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Available actions:
- Handle a sweep wake cycle (precheck woke with `data.meetings`) — create the prepared drive blocks and notify the user
- Handle a skip reply (user said "skip `<meeting_id>`") — remove that meeting's drive blocks and record the skip

Skill bundle scripts run from the runtime mount `/home/node/.claude/skills/tessl__drive-planner/`. Routing and the canonical home address are resolved by the precheck; `maps_client` ships in the co-located `tessl__flight-assist` bundle and is imported by the scripts, not invoked here.

## Step 1 — Handle a sweep wake cycle

This step fires when the precheck wakes the agent with a `data.meetings` payload. Each entry is one in-person meeting that needs a drive block, carrying `meeting_id`, `summary`, `start`, `location`, display-ready `leave_by` and `drive_minutes`, the prepared `create_args` (one per leg), `route_errors`, and `unplannable` (legs the precheck refused to block because the drive can't be real — too far, or it overruns the gap between meetings; the operator likely flew). The blocks are create-first: create them, then tell the user they can skip.

First create the blocks. Pass the whole `data` object (it already has the `meetings` array) to the apply script in `create` mode:

```bash
echo '<data JSON>' | python3 /home/node/.claude/skills/tessl__drive-planner/apply.py create
```

It is idempotent — a meeting whose block already exists is skipped, never duplicated (lombot #50). It prints single-line JSON: `{"created": [...], "skipped_existing": [...], "failed": [...]}`.

Then compose ONE Telegram notification via `mcp__nanoclaw__send_message` summarizing what changed:

- For each meeting that got ANY created block (`created` lists `outbound` / `bridge` / `return` legs): "Added drive block for `<summary>` — leave by `<leave_by>` (`<drive_minutes>`-min drive with current traffic). Reply `skip <meeting_id>` if you're not driving." Use the meeting's `leave_by` and `drive_minutes` fields verbatim; when both are null (the only created leg is a `return`), phrase it "Added a return drive block for `<summary>`." Phrase relative-date words per `rules/operator-local-tz-phrasing.md`.
- If a meeting carries `route_errors`, add a line: "Couldn't compute drive time for `<summary>` (`<error>`) — no block created; will retry next sweep."
- If a meeting carries `unplannable` legs, add one line per leg, in the order listed (naming the leg's `direction` keeps it accurate when other legs of the same meeting still got a block — e.g. a `bridge` is gated but the `return` is created): "No `<direction>` drive block for `<summary>` — `<reason>`." Use each leg's `reason` verbatim (it already says whether it's too far or doesn't fit the gap); don't add your own cause. Then add one "Reply `skip <meeting_id>` to stop seeing it." for the meeting.
- If `apply` reported `failed` legs, add a line naming the meeting and the error.

Silence rule: if `created`, `route_errors`, `unplannable`, and `failed` are all empty (every surfaced meeting was already handled), send nothing — proceed silently. Finish here.

## Step 2 — Handle a skip reply

This step fires when the user replies "skip `<meeting_id>`" (or "don't drive to `<meeting_id>`") about a drive block the sweep created. Remove the blocks and record the skip so the next sweep does not recreate them.

```bash
echo '{"meeting_id": "<meeting_id>", "now": "<current ISO-8601, tz-aware>"}' \
  | python3 /home/node/.claude/skills/tessl__drive-planner/apply.py remove
```

`now` is the current time as a timezone-aware ISO-8601 string. The script deletes the meeting's drive blocks and records a skip (it computes the skip's expiry itself — see `apply.py` / `state-schema.md`) so the next sweep won't recreate them, then prints `{"removed": [...], "skip_recorded": true}`.

Confirm to the user via `mcp__nanoclaw__send_message`: "Removed the drive block for `<meeting_id>` — won't plan it again." If `removed` is empty (the block was already gone), still confirm the skip was recorded so a later sweep won't recreate it. Finish here.
