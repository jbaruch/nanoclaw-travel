---
name: drive-planner-recheck
description: "Traffic-growth watcher for drive-planner blocks. On a ~15-min precheck poll it re-routes each in-window drive block and, when traffic has grown enough that the user must leave earlier — or it is already time to go — pushes a leave-earlier / leave-now alert. Use on a drive-planner recheck wake event. Triggers - 'drive recheck alert', 'leave earlier for <meeting>', 'leave now for <meeting>', 'traffic grew for my drive'."
cadence: "*/15 * * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "precheck.py"
---

# Drive Planner Recheck

Process steps in order. Do not skip ahead.

When the recheck precheck wakes this skill, push the alerting blocks to the user, then record the suppression so the same condition is not re-pushed. All routing and the keep-quiet/alert decision happen in the precheck (`precheck.py`); it only wakes the agent when at least one block must alert. The suppression is recorded AFTER the send (Step 2), never before — a record written before a failed send would permanently drop a leave-earlier / leave-now alert.

## Step 1 — Push the leave-earlier / leave-now alert

The precheck woke with a `data.alerts` payload. Each entry is one drive block the precheck has already decided must alert, carrying `summary`, `kinds` (`growth` and/or `leave_now`), display-ready `current_minutes` and `delta_minutes`, and `new_leave_by`.

Compose ONE Telegram notification via `mcp__nanoclaw__send_message`, one line per alert, using the payload's fields verbatim:

- When `kinds` contains `leave_now`: "Leave now for `<summary>` — `<current_minutes>`-min drive in current traffic, you're at the leave-by."
- Otherwise (`growth` only): "Traffic building for `<summary>` — drive now `<current_minutes>` min (up `<delta_minutes>`). Leave by `<new_leave_by>` to stay on time."

Phrase any relative-date words per `rules/operator-local-tz-phrasing.md`; displayed clock times stay as-is. The precheck also wakes when `data.route_errors` is non-empty (a due block whose traffic it couldn't check) — for each, append one line: "Couldn't check traffic for `<destination>` (`<error>`) — will retry next poll."

If both `data.alerts` and `data.route_errors` are empty, send nothing and finish here. If there were `alerts`, once the send has gone out proceed to Step 2; a route-errors-only wake has no suppression to record, so finish here.

## Step 2 — Record the suppression (only after the send)

Only after Step 1's `mcp__nanoclaw__send_message` has delivered the alert, persist the suppression so the next poll does not re-push the same condition. Pass the precheck's `data` (it carries the `patches` array) to the apply script in `suppress` mode:

```bash
echo '<data JSON>' | python3 /home/node/.claude/skills/tessl__drive-planner/apply.py suppress
```

Each patch carries the block's full rebuilt `description` (the machine state lives in the description) with the alert record updated; the script PATCHes it back. It prints `{"patched": [...]}`. If the send in Step 1 failed, do NOT run this step — leaving the block unsuppressed re-pings next poll, which is the safe direction. Finish here.
