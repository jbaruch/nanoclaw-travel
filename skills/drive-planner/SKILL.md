---
name: drive-planner
description: "Ground-transit drive planner for in-person meetings. On a ~2h precheck sweep it creates a traffic-aware Free drive block (home → venue → home) for each in-person meeting that lacks one and tells the user, who can reply to cancel; the recheck poll then watches each block for traffic growth. Use on a drive-planner sweep wake event, or when the user replies to cancel a drive block. Triggers - 'drive block', 'plan my drive', 'skip', 'skip 1', 'skip 1 and 3', 'cancel 2', 'cancel that drive', 'don't drive to that meeting', 'remove drive block', 'drive to my meeting', 'leave-by for a meeting'."
cadence: "0 */2 * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "precheck.py"
---

# Drive Planner

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Available actions:
- Handle a sweep wake cycle (precheck woke with `data.meetings`) — create the prepared drive blocks and notify the user
- Handle a skip reply (user said "skip", "skip 1", "skip 1 and 3", "cancel 2", or "don't drive to `<meeting>`") — remove the referenced meeting's drive blocks and record the skip

Never put an internal calendar event/meeting id in a user-facing message, and never require the user to type one. The user refers to a block by its list number or the meeting name; the skill maps that to the id itself.

Skill bundle scripts run from the runtime mount `/home/node/.claude/skills/tessl__drive-planner/`. Routing and the canonical home address are resolved by the precheck; `maps_client` ships in the co-located `tessl__flight-assist` bundle and is imported by the scripts, not invoked here.

## Step 1 — Handle a sweep wake cycle

This step fires when the precheck wakes the agent with a `data.meetings` payload. Each entry is one in-person meeting that needs a drive block, carrying `meeting_id`, `summary`, `start`, `location`, display-ready `leave_by` and `drive_minutes`, the prepared `create_args` (one per leg), `route_errors`, and `unplannable` (legs the precheck would not block, each carrying a `direction`, `drive_minutes`, and a display-ready `reason`). The blocks are create-first: create them, then tell the user they can skip.

First create the blocks. Pass the whole `data` object (it already has the `meetings` array) to the apply script in `create` mode:

```bash
echo '<data JSON>' | python3 /home/node/.claude/skills/tessl__drive-planner/apply.py create
```

It is idempotent — a meeting whose block already exists is skipped, never duplicated (lombot #50). It prints single-line JSON: `{"created": [...], "skipped_existing": [...], "failed": [...], "message": "<text or null>"}`.

The `message` field is the complete, ready-to-send notification — the script builds it deterministically (`apply.py build_notification`), id-free, with the skip affordance baked in: one created block → a single sentence ending "Reply `skip` if you're not driving."; several → a numbered list ending "Reply `skip 1`, or `skip 1 and 3`, to drop any."; plus any route-error / unplannable / failed lines. Relay `message` **verbatim** via `mcp__nanoclaw__send_message` — do not rewrite, renumber, add a meeting/event id, or append the meeting start time.

Silence rule: when `message` is `null`, every surfaced meeting was already handled — send nothing, proceed silently. Finish here.

## Step 2 — Handle a skip reply

This step fires when the user replies to skip a drive block — a bare "skip" / "don't drive", a numbered "skip 1" / "skip 1 and 3" / "skip 1, 3", or by meeting name ("don't drive to swimming"). "cancel" is accepted as a synonym for "skip" in all these forms. The user never types an id; resolve their reference to a meeting NAME, then let the script find the id.

Resolve the reference to one or more meeting names:

- A bare "skip" / "don't drive" refers to the single block you just announced.
- A number or list of numbers ("skip 1", "skip 1 and 3", "skip 1, 3", "cancel 2") refers to the numbered lines in YOUR prior sweep notification (read them from the conversation) — take the meeting name from each referenced line. The number is only an index into that one notification; never index by calendar order.
- A meeting name ("don't drive to swimming") is the name directly.
- If you can't tell which meeting is meant, run `apply.py list` — it prints `{"blocks": [{"summary": "...", "meeting_id": "...", "leave_by": "<ISO>"}]}` — and ask the user which one they mean, showing each block's `summary` and `leave_by` (never the `meeting_id`).

Remove each resolved meeting by name. Pass its `leave_by` (from your prior notification or `apply.py list`) so the right instance is picked when meetings share a name; `now` is the current tz-aware ISO-8601 time:

```bash
echo '{"summary": "<meeting name>", "leave_by": "<ISO from the notification/list>", "now": "<current ISO-8601, tz-aware>"}' \
  | python3 /home/node/.claude/skills/tessl__drive-planner/apply.py remove
```

The script resolves the name to that meeting's id itself — by exact name, independent of calendar order — deletes its drive blocks, and records a skip (it computes the expiry itself — see `apply.py` / `state-schema.md`) so the next sweep won't recreate them. A meeting with no block (an `unplannable` one) still records the skip via its calendar event. It prints one of:

- `{"removed": [...], "skip_recorded": true}` — done.
- `{"removed": [], "skip_recorded": false, "unmatched_summary": "..."}` — nothing matched.
- `{"removed": [], "skip_recorded": false, "ambiguous_summary": "...", "candidates": [{"summary": "...", "leave_by": "<ISO>"}]}` — several meetings share the name; ask the user which `leave_by` they mean, then re-run with that `leave_by`.

Confirm to the user via `mcp__nanoclaw__send_message` by meeting name, never id. When `removed` lists blocks: "Removed the drive block for `<summary>` — won't plan it again." When `removed` is empty but `skip_recorded` is true: "Won't plan a drive to `<summary>`." On `unmatched_summary`, say you couldn't find that meeting and ask which they mean (names + leave-by from `apply.py list`). Finish here.
