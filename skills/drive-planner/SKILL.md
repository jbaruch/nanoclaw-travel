---
name: drive-planner
description: "Ground-transit drive planner for in-person meetings. On a ~2h precheck sweep it creates a traffic-aware Free drive block (home → venue → home) for each in-person meeting that lacks one and tells the user, who can reply to cancel; the recheck poll then watches each block for traffic growth. Use on a drive-planner sweep wake event, or when the user replies to cancel a drive block. Triggers - 'drive block', 'plan my drive', 'cancel 2', 'cancel that drive', 'skip', 'don't drive to that meeting', 'remove drive block', 'drive to my meeting', 'leave-by for a meeting'."
cadence: "0 */2 * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "precheck.py"
---

# Drive Planner

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Available actions:
- Handle a sweep wake cycle (precheck woke with `data.meetings`) — create the prepared drive blocks and notify the user
- Handle a cancel reply (user said "cancel 2", "cancel 1,3", "skip", or "don't drive to `<meeting>`") — remove the referenced meeting's drive blocks and record the skip

Never put an internal calendar event/meeting id in a user-facing message, and never require the user to type one. The user refers to a block by its list number or the meeting name; the skill maps that to the id itself.

Skill bundle scripts run from the runtime mount `/home/node/.claude/skills/tessl__drive-planner/`. Routing and the canonical home address are resolved by the precheck; `maps_client` ships in the co-located `tessl__flight-assist` bundle and is imported by the scripts, not invoked here.

## Step 1 — Handle a sweep wake cycle

This step fires when the precheck wakes the agent with a `data.meetings` payload. Each entry is one in-person meeting that needs a drive block, carrying `meeting_id`, `summary`, `start`, `location`, display-ready `leave_by` and `drive_minutes`, the prepared `create_args` (one per leg), `route_errors`, and `unplannable` (legs the precheck would not block, each carrying a `direction`, `drive_minutes`, and a display-ready `reason`). The blocks are create-first: create them, then tell the user they can skip.

First create the blocks. Pass the whole `data` object (it already has the `meetings` array) to the apply script in `create` mode:

```bash
echo '<data JSON>' | python3 /home/node/.claude/skills/tessl__drive-planner/apply.py create
```

It is idempotent — a meeting whose block already exists is skipped, never duplicated (lombot #50). It prints single-line JSON: `{"created": [...], "skipped_existing": [...], "failed": [...]}`.

Then compose ONE Telegram notification via `mcp__nanoclaw__send_message` summarizing what changed. List the meetings that got blocks in `leave_by` order, and never include a `meeting_id` — the cancel reply works by list number or meeting name. Phrase relative-date words per `rules/operator-local-tz-phrasing.md`, and use each meeting's `leave_by` / `drive_minutes` fields verbatim.

- When exactly ONE meeting got a created block: "Added a drive block for `<summary>` — leave by `<leave_by>` (`<drive_minutes>`-min drive with current traffic). Reply `skip` or `don't drive` to cancel." When the only created leg is a `return` (`leave_by` / `drive_minutes` null): "Added a return drive block for `<summary>` — reply `skip` to cancel."
- When SEVERAL meetings got blocks: number them and let the user cancel by number. Open with "Added drive blocks:", then one numbered line each — "`<n>`. `<summary>` — leave by `<leave_by>` (`<drive_minutes>`-min drive)" — then close with "Reply `cancel 2` or `cancel 1,3` to drop any." Keep the numbering for the cancel step (it matches `leave_by` order).
- If a meeting carries `route_errors`, add a line: "Couldn't compute drive time for `<summary>` (`<error>`) — no block created; will retry next sweep."
- If a meeting carries `unplannable` legs, add one line per leg, in the order listed, naming the leg's `direction` (so it stays accurate when another leg of the same meeting still got a block): "No `<direction>` drive block for `<summary>` — `<reason>`." Use each leg's `reason` verbatim; don't add your own cause.
- If `apply` reported `failed` legs, add a line naming the meeting and the error.

Silence rule: if `created`, `route_errors`, `unplannable`, and `failed` are all empty (every surfaced meeting was already handled), send nothing — proceed silently. Finish here.

## Step 2 — Handle a cancel reply

This step fires when the user replies to cancel a drive block — by list number ("cancel 2", "cancel 1,3"), a plain "skip" / "don't drive", or by meeting name ("don't drive to swimming"). The user never types an id; resolve their reference to the internal `meeting_id` here.

First list the current drive blocks (one per meeting, ordered by `leave_by`, the same order the sweep notification numbered them):

```bash
echo '{"now": "<current ISO-8601, tz-aware>"}' \
  | python3 /home/node/.claude/skills/tessl__drive-planner/apply.py list
```

It prints `{"blocks": [{"summary": "...", "meeting_id": "...", "leave_by": "<ISO>"}]}`. Map the user's reference onto these blocks to get the `meeting_id`(s):

- A number ("cancel 2") or list of numbers ("cancel 1,3") indexes the numbered list from your prior notification — match those summaries to the `blocks` here. A bare "skip" / "don't drive" when there is exactly one block means that block.
- A meeting name ("don't drive to swimming") matches the block whose `summary` names it.
- If the reference is ambiguous or matches nothing (the list changed since the notification), ask the user which meeting, listing the current block `summary` values — never the ids.

Then remove each resolved meeting. `now` is the current tz-aware ISO-8601 time:

```bash
echo '{"meeting_id": "<resolved meeting_id>", "now": "<current ISO-8601, tz-aware>"}' \
  | python3 /home/node/.claude/skills/tessl__drive-planner/apply.py remove
```

The script deletes that meeting's blocks and records a skip (it computes the expiry itself — see `apply.py` / `state-schema.md`) so the next sweep won't recreate them, printing `{"removed": [...], "skip_recorded": true}`.

Confirm to the user via `mcp__nanoclaw__send_message` by meeting name, never id: "Removed the drive block for `<summary>` — won't plan it again." When `removed` is empty (the block was already gone), still confirm: "Won't plan a drive to `<summary>`." The skip is recorded either way. Finish here.
