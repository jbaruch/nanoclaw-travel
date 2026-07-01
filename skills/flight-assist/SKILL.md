---
name: flight-assist
description: On a byAir precheck wake event, reconciles the operator's managed calendar events (boarding block, adopted byAir flight event, Reclaim travel-block cleanup, switched-away teardown) and composes a user-facing flight notification — delay, gate change, cancellation, boarding, connection risk, inbound-delay, time-to-leave, baggage carousel, day-before check, or arrival logistics. Also configures the plugin (verify credentials, set home base). Use when a tracked-flight wake event needs a notification, or when setting up or diagnosing flight-assist. Triggers - "check flight-assist env", "diagnose flight-assist", "set flight-assist home base", "set home address", "configure flight-assist", "flight delay notification", "gate change notification", "cancellation notification", "boarding alert", "time to leave alert", "inbound delay notification", "baggage carousel", "arrival logistics", "day before sanity check", "flight removed upstream", "connection at risk", "tight connection alert", "reconcile calendar".
cadence: "*/2 * * * *"
agentModel: "claude-haiku-4-5-20251001"
script: "precheck.py"
---

# Flight Assist

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Available actions:
- Diagnose env (verify byAir + Google Maps credentials)
- Set home base (record the user's home address for time-to-leave)
- Handle a precheck wake cycle (reconcile managed calendar events, then compose a user-facing notification)

## Step 1 — Diagnose env

Run the env-presence check (`scripts/check-env.py` relative to this skill; the NanoClaw runtime mounts every `tessl__*` skill at `/home/node/.claude/skills/tessl__<skill-name>/`, so the absolute path the agent must literally invoke is):

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/check-env.py
```

The script reads `BYAIR_MCP_URL`, `GOOGLE_MAPS_API_KEY`, `COMPOSIO_API_KEY`, and `COMPOSIO_USER_ID`, prints single-line JSON on stdout, exits 0.

Parse the JSON. All `true`: emit `flight-assist credentials present`. Any `false`: emit one line per missing variable:

- `BYAIR_MCP_URL missing. Add personal MCP link from https://byairapp.com/mcp/ to OneCLI vault and restart container.`
- `GOOGLE_MAPS_API_KEY missing. Create a Distance Matrix API key at https://console.cloud.google.com/apis/credentials and add to OneCLI vault.`
- `COMPOSIO_API_KEY missing. Add the Composio project API key from https://app.composio.dev settings to OneCLI vault — calendar reconciliation is disabled without it.`
- `COMPOSIO_USER_ID missing. Add the Composio user/entity the Google Calendar account is connected under to OneCLI vault — calendar reconciliation is disabled without it.`

Finish here.

## Step 2 — Set home base

When the user provides their home address (e.g., "set my home base to 1 Infinite Loop, Cupertino, CA"), invoke the `set-home-base.py` script to persist it to the plugin-wide config the precheck reads for `time_to_leave` queries as the fallback origin when no fresh live-location snapshot is available (see Step 3's `time_to_leave` row for the origin-resolution ladder).

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/set-home-base.py "<address from user>"
```

The script writes the address to `/workspace/state/flight-assist/config.json` (atomic, idempotent), preserves any other config keys, and prints `{"home_address": "..."}` to stdout. Exit 0 on success, exit 2 on usage/validation failure.

Emit one confirmation line to the user: `Home base set: <address>`. Finish here.

## Step 3 — Handle the precheck wake cycle

This step fires when the precheck script wakes the agent with a `data.events` payload. The wake cycle does two things in order: first reconcile the managed calendar events (deterministic glue, no LLM), then compose one human-readable notification per event.

First, run the calendar reconcile once per wake cycle, before composing notifications:

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/reconcile.py
```

It converges the calendar to byAir truth — creates/shifts the boarding block, adopts and delta-shifts the byAir flight event, removes Reclaim travel blocks inside a same-airport layover gap, and tears down events for switched-away / cancelled flights that dropped out of the active-flights index. It also reconciles the airport drive blocks (#90) on the primary calendar — the drive to the departure airport before the flight leaves and the drive home after it lands, anchored on the clearance / post-arrival policy and current traffic. Every write is idempotent and a no-op when the calendar already matches, so it is safe to run on every wake cycle and alongside byAir's own delay-shifts.

It prints single-line JSON: `{"status": "...", "planned": N, "executed": N, "archived": N, "failed": [...], "airport_drive": {...}}`. Output keys vary by `status`: `byair_calendar_id` and `archived` are present only when a cycle ran (`ok` / `no_flights`) and are omitted on `no_calendar`, so do not depend on a key that may be absent. `status: "ok"` means a cycle ran. `no_calendar` (no flight calendar resolved from config), `no_flights` (nothing tracked), or `{"status": "error", "error": "credentials"}` (Composio not configured — see Step 1) all mean reconciliation is inactive this cycle. The `airport_drive` object is the same shape for the drive-block reconcile and is present on every non-error summary (`ok` / `no_calendar` / `no_flights`) — independently `{"status": "ok", ...}` even when the top-level `status` is `no_calendar`, since drive blocks live on the primary calendar — but is absent on the early `{"status": "error", "error": "credentials"}` / `"state"` setup-failure outputs, which return before it runs. It stays a dormant zero-op summary without a Maps key / byAir URL / tracked flights, and its own `status` can be `error` (a transient byAir/Maps/Composio failure, or `error: "state"` on corrupt state) without failing the overall cycle — treat it as bookkeeping either way. Treat any of those, and a non-empty `failed` list (those ops retry next cycle), as expected: proceed to the notification below. Reconcile output is calendar bookkeeping, never a user-facing message — do not surface it; proceed silently regardless of its result.

Then compose the notification. Each event is `{"flight_id": int, "event": {"reason": "...", ...}}`. Compose one human-readable notification per event.

The full event-shape contract is in `references/event-payloads.md`; consult it when an event's `reason` is unfamiliar.

Phrase relative-date words ("today" / "tomorrow") against the operator's local date per `rules/operator-local-tz-phrasing.md` (run `/home/node/.claude/skills/tessl__flight-assist/scripts/read-current-tz.py`). Displayed airport clock times stay as-is.

The reason → notification mapping for the documented events:

| `reason` | Notification |
|----------|-------------|
| `cancelled` | "Flight `<code>` cancelled." Include rebooking-options link if the user opted in |
| `diverted` | "Flight `<code>` diverted." |
| `gate_assignment` | One-time gate + terminal readout when the pre-boarding window opens: "`<code>` departs Terminal `<dep_terminal>`, Gate `<dep_gate>`." Drop the terminal clause when `dep_terminal` is null: "`<code>` departs Gate `<dep_gate>`." This is the navigation signal — which terminal to head to |
| `gate_change` | "Gate change: `<code>` moved from `<from>` to `<to>`." If `to` is null, "Gate `<from>` removed from `<code>`." |
| `delay` | "Flight `<code>` delayed by `<delay_minutes>` min. New departure: `<new_dep_time>` (local)." Negative `delay_minutes` = advanced; phrase as "moved earlier by N min". A `schedule_slip: true` event is the same surface — phrase it identically |
| `inbound_delay_predicted` | "Inbound aircraft delay predicted: `<delay_minutes>` min for `<code>`. New estimated departure: `<predicted_time>`." |
| `inbound_delay_retracted` | All-clear after a previously-surfaced inbound delay: "Inbound delay for `<code>` has cleared — previously predicted `<prev_delay_minutes>` min, now `<new_delay_minutes>` min (or no longer predicted). `<code>` is back on track." Always send this even if a prior "rebook now" / connection surface went out — silence is read as still-delayed |
| `connection_at_risk` | When `missed_connection` is true (transfer window has hit zero or gone negative): "Connection structurally missed: `<leg1_code>` projected to arrive at or after `<leg2_code>` departs. Rebook required." Otherwise: "Tight connection: `<leg2_code>` boards in `<transfer_minutes_remaining>` min after `<leg1_code>` arrives — below the `<min_transfer_minutes>`-min buffer. Consider rebooking now." `flight_id` on this event refers to leg-2 (the downstream leg) |
| `boarding_started` | "Boarding now: `<code>`. Gate `<dep_gate>`, terminal `<dep_terminal>`." |
| `carousel_revealed` | "Baggage carousel for `<code>`: `<baggage>`." |
| `day_before` | Day-before sanity check: read the user's calendar via MCP if available, list any events that overlap the flight window (T-3h before dep through T+3h after arr), and summarize. Read flight state via `read_flight_state(flight_id)` for context. Label the day per `rules/operator-local-tz-phrasing.md` |
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
