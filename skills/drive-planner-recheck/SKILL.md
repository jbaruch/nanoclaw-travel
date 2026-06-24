---
name: drive-planner-recheck
description: "Traffic-growth watcher for drive-planner blocks. On a ~15-min precheck poll it re-routes each in-window drive block and, when traffic has grown enough that the user must leave earlier — or it is already time to go — pushes a leave-earlier / leave-now alert. Use on a drive-planner recheck wake event. Triggers - 'drive recheck alert', 'leave earlier for <meeting>', 'leave now for <meeting>', 'traffic grew for my drive'."
cadence: "*/15 * * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "precheck.py"
---

# Drive Planner Recheck

Process steps in order. Do not skip ahead.

This skill has one job: when the recheck precheck wakes it, turn the alerting blocks into a user-facing push. All the routing and the keep-quiet/alert decision happen in the precheck (`precheck.py`); the precheck only wakes the agent when at least one block must alert, and it has already recorded the suppression so the same condition is not re-pushed. The agent composes the message — nothing more.

## Step 1 — Push the leave-earlier / leave-now alert

The precheck woke with a `data.alerts` payload. Each entry is one drive block whose traffic grew past the threshold or whose leave-by has arrived, carrying `summary`, `kinds` (`growth` and/or `leave_now`), `current_seconds`, `delta_seconds`, `new_leave_by`, and `seconds_until_leave_by`.

Compose ONE Telegram notification via `mcp__nanoclaw__send_message`, one line per alert:

- When `kinds` contains `leave_now`: "Leave now for `<summary>` — `<current_minutes>`-min drive in current traffic, you're at the leave-by." (`<current_minutes>` = `current_seconds` ÷ 60.)
- Otherwise (`growth` only): "Traffic building for `<summary>` — drive now `<current_minutes>` min (up `<delta_minutes>`). Leave by `<new_leave_by>` to stay on time." (`<delta_minutes>` = `delta_seconds` ÷ 60.)

Phrase any relative-date words per `rules/operator-local-tz-phrasing.md`; displayed clock times stay as-is. If `data.route_errors` is non-empty, append one line: "Couldn't check traffic for `<destination>` (`<error>`) — will retry next poll."

If `data.alerts` is empty (the precheck would not normally wake without one), send nothing. Finish here.
