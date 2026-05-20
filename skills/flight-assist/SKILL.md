---
name: flight-assist
description: Flight notifications and prep for tracked trips. Diagnose env, set home base, or compose a user-facing message from a precheck wake event. Triggers - "check flight-assist env", "diagnose flight-assist", "set flight-assist home base", "set home address", "configure flight-assist", "flight delay notification", "gate change notification", "cancellation notification", "boarding alert", "time to leave alert", "inbound delay notification", "baggage carousel", "arrival logistics", "day before sanity check", "flight removed upstream", "connection at risk", "tight connection alert".
cadence: "*/2 * * * *"
script: "precheck.py"
---

# Flight Assist

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Available actions:
- Diagnose env (verify byAir + Google Maps credentials)
- Set home base (record the user's home address for time-to-leave)
- Compose a user-facing notification from a precheck wake event

## Step 1 — Diagnose env

Run the env-presence check (`scripts/check-env.py` relative to this skill; the NanoClaw runtime mounts every `tessl__*` skill at `/home/node/.claude/skills/tessl__<skill-name>/`, so the absolute path the agent must literally invoke is):

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/check-env.py
```

The script reads `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`, prints single-line JSON on stdout, exits 0.

Parse the JSON. Both `true`: emit `flight-assist credentials present`. Either `false`: emit one line per missing variable:

- `BYAIR_MCP_URL missing. Add personal MCP link from https://byairapp.com/mcp/ to OneCLI vault and restart container.`
- `GOOGLE_MAPS_API_KEY missing. Create a Distance Matrix API key at https://console.cloud.google.com/apis/credentials and add to OneCLI vault.`

Finish here.

## Step 2 — Set home base

When the user provides their home address (e.g., "set my home base to 1 Infinite Loop, Cupertino, CA"), invoke the `set-home-base.py` script to persist it to the tile-wide config the precheck reads for `time_to_leave` queries as the fallback origin when no fresh live-location snapshot is available (see Step 3's `time_to_leave` row for the origin-resolution ladder).

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/set-home-base.py "<address from user>"
```

The script writes the address to `/workspace/state/flight-assist/config.json` (atomic, idempotent), preserves any other config keys, and prints `{"home_address": "..."}` to stdout. Exit 0 on success, exit 2 on usage/validation failure.

Emit one confirmation line to the user: `Home base set: <address>`. Finish here.

## Step 3 — Compose wake event notification

This step fires when the precheck script wakes the agent with a `data.events` payload. Each event is `{"flight_id": int, "event": {"reason": "...", ...}}`. Compose one human-readable notification per event.

The full event-shape contract is in `references/event-payloads.md`; consult it when an event's `reason` is unfamiliar. The reason → notification mapping for the documented events:

| `reason` | Notification |
|----------|-------------|
| `cancelled` | "Flight `<code>` cancelled." Include rebooking-options link if the user opted in |
| `diverted` | "Flight `<code>` diverted." |
| `gate_change` | "Gate change: `<code>` moved from `<from>` to `<to>`." If `to` is null, "Gate `<from>` removed from `<code>`." |
| `delay` | "Flight `<code>` delayed by `<delay_minutes>` min. New departure: `<new_dep_time>` (local)." Negative `delay_minutes` = advanced; phrase as "moved earlier by N min" |
| `inbound_delay_predicted` | "Inbound aircraft delay predicted: `<delay_minutes>` min for `<code>`. New estimated departure: `<predicted_time>`." |
| `connection_at_risk` | When `missed_connection` is true (transfer window has hit zero or gone negative): "Connection structurally missed: `<leg1_code>` projected to arrive at or after `<leg2_code>` departs. Rebook required." Otherwise: "Tight connection: `<leg2_code>` boards in `<transfer_minutes_remaining>` min after `<leg1_code>` arrives — below the `<min_transfer_minutes>`-min buffer. Consider rebooking now." `flight_id` on this event refers to leg-2 (the downstream leg) |
| `boarding_started` | "Boarding now: `<code>`. Gate `<dep_gate>`, terminal `<dep_terminal>`." |
| `carousel_revealed` | "Baggage carousel for `<code>`: `<baggage>`." |
| `day_before` | Day-before sanity check: read the user's calendar via MCP if available, list any events that overlap the flight window (T-3h before dep through T+3h after arr), and summarize. Read flight state via `read_flight_state(flight_id)` for context |
| `time_to_leave` | "Leave by `<leave_by>` to make `<code>` (`<travel_time_minutes>` min drive with current traffic)." Origin resolution ladder lives in `state-schema.md` under `current-location.json` |
| `arrival_logistics` | Surface baggage carousel (read from `last_snapshot.baggage`), suggest a rideshare ETA if location is available, and note lounge access if connecting. Read flight state for context |
| `removed_upstream` | "Flight `<code>` no longer tracked upstream — remove from active trips if intentional." Don't auto-delete; let the user confirm |
| `tracked_flight_added` | Silent in most cases (the daily sync added a flight to the tracking index). Surface only when the user explicitly asked "what's new" or when `data.events` contains nothing else worth reporting |
| `tracked_flight_removed` | "Flight `<code>` stopped tracking — daily sync removed it from the active-flights index." Treat as informational, not actionable |

For each event, fetch the per-flight state to enrich the notification (gate, terminal, ETD, ETA) by invoking `get-flight-state.py`:

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/get-flight-state.py <flight_id>
```

Outputs the full state record as single-line JSON on stdout, or `{"error": "..."}` when the flight has no state on disk. Use the `last_snapshot.dep_gate`, `last_snapshot.arr_gate`, `dep_terminal`, etc. to fill in the notification template.

If multiple events share the same `flight_id` (e.g., `gate_change` + `delay` in one cycle), compose ONE notification per flight that merges them, ordered: cancel/divert first, then time-sensitive (boarding, time_to_leave), then info (gate, delay, inbound), then logistics (carousel, arrival).

After composing, route the notification through the user's main channel (the orchestrator handles routing; the skill just produces the message text).

If no actionable text would result for an event (e.g., a duplicate notification you've already sent within the last 10 min), proceed silently — do not echo "no notification needed". Finish here.
