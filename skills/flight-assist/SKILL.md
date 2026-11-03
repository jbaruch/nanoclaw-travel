---
name: flight-assist
description: Flight assist for tracked flights. Its precheck polls byAir every 2 minutes, sends templated flight notices (delay, gate change, cancellation, boarding, connection risk, inbound delay, time to leave, baggage carousel, day-before check, arrival logistics) through the host itself, and reconciles the managed calendar events. The model is never woken for a notice. Use to configure or diagnose the plugin — 'diagnose flight-assist', 'set flight-assist home base', 'configure flight-assist', 'why didn't I get a flight notice'.
cadence: "*/2 * * * *"
# agentModel renewal: no scanner tracks this pin. Re-check it on each new Claude model release and bump it in its own PR.
agentModel: "claude-haiku-5-5"
script: "precheck.py"
precheck_timeout_ms: 75000
---

# Flight Assist

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Available actions:
- Diagnose env (verify byAir + Google Maps credentials)
- Set home base (record the user's home address for time-to-leave)
- Explain a missing notice (read the precheck's delivery result)

## Step 1 — Diagnose env

Run the env-presence check (`scripts/check-env.py` relative to this skill; the NanoClaw runtime mounts every `tessl__*` skill at `/home/node/.claude/skills/tessl__<skill-name>/`, so the absolute path the agent must literally invoke is):

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/check-env.py
```

The script reads `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`, prints single-line JSON on stdout, exits 0.

Parse the JSON. All `true`: emit `flight-assist credentials present`. Any `false`: emit one line per missing variable:

- `BYAIR_MCP_URL missing. Add personal MCP link from https://byairapp.com/mcp/ to OneCLI vault and restart container.`
- `GOOGLE_MAPS_API_KEY missing. Create a Distance Matrix API key at https://console.cloud.google.com/apis/credentials and add to OneCLI vault.`
Calendar access has no flag: OneCLI's gateway injects the Google OAuth Bearer on the wire, so no credential lives in this container and no env var gates it (nanoclaw#638). If the user is diagnosing calendar writes specifically, tell them to run the reconcile (Step 3) and read its `error` — `gateway` means the gateway is not authenticating the requests, `tier` means this agent's tier is gated from Google by design.

Finish here.

## Step 2 — Set home base

When the user provides their home address (e.g., "set my home base to 1 Infinite Loop, Cupertino, CA"), invoke the `set-home-base.py` script to persist it to the plugin-wide config the precheck reads for `time_to_leave` queries as the fallback origin when no fresh live-location snapshot is available (see Step 3's `time_to_leave` row for the origin-resolution ladder). While a TripIt trip is active the precheck swaps the current trip lodging in for this address (`trip_origin.py`, #122) — the config value is the off-trip base, not a hard override.

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/set-home-base.py "<address from user>"
```

The script writes the address to `/workspace/state/flight-assist/config.json` (atomic, idempotent), preserves any other config keys, and prints `{"home_address": "..."}` to stdout. Exit 0 on success, exit 2 on usage/validation failure.

Emit one confirmation line to the user: `Home base set: <address>`. Finish here.

## Step 3 — Explain a missing notice

The precheck delivers every notice itself (#370): it renders the event's template (`notices.py`), sends it through the host and confirms it in `notifications.json` only on the host's success. It then runs `scripts/reconcile.py` for the calendar. Its run log `data` carries `sent`, `send_failed` (the notification ids still pending) and `reconcile`. A pending notice is offered again after five minutes until it expires.

To see a flight's current state:

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/get-flight-state.py <flight_id>
```

To rerun the calendar reconcile by hand:

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/reconcile.py
```

Report what the run log and state show: the notice went out, is still pending, or never fired. Never send a flight notice yourself. Finish here.
