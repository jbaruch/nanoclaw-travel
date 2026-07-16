---
name: nightly-travel-sync
description: "Travel-data refresh bundle: TripIt → Reclaim timezone sync, refresh travel-schedule.json from the TripIt iCal feed with a two-tier Gmail freshness probe, rebuild travel-db.json, then check upcoming trips for booking gaps. Runs daily; precheck-gated on travel-db.json freshness. Triggers: 'sync trips', 'sync travel', 'update travel data', 'pull trip info', 'refresh travel schedule', 'rebuild travel db', 'check my bookings'."
cadence: "0 6 * * * (TZ=local)"
agentModel: "claude-haiku-4-5-20251001"
script: "precheck.py"
---

# Nightly Travel Sync

Process steps in order. Do not skip ahead.

Run this bundle silently. Each step surfaces its own results via `mcp__nanoclaw__send_message`; the bundle itself adds no chat surface. The fire-time precheck (`precheck.py`) gates wake-ups on `travel-db.json` freshness — see `precheck.py` for the cadence predicate and threshold.

A step that hits a technical failure surfaces a one-line note and finishes the run. There is no mid-run continuation. Recovery is the next daily cron fire.

## Step 1 — TripIt → Reclaim sync

`mcp__nanoclaw__sync_tripit()`. Report changes (new timezones, OOO blocks). Flag overlapping trips as a warning. Surface tool errors.

If the MCP call fails, surface the error via `mcp__nanoclaw__send_message`, emit `<internal>nightly-travel-sync exited step-1: mcp-fail</internal>` as your final turn text, and finish. Otherwise proceed to Step 2.

## Step 2 — Refresh travel-schedule.json from TripIt ICS

```bash
python3 /home/node/.claude/skills/tessl__nightly-travel-sync/scripts/refresh-travel-schedule.py
```

Pulls live ICS from `/workspace/group/tripit-url.txt`, writes `/workspace/group/travel-schedule.json` (record shape + reader contract in `state-schema.md`). On non-zero exit (TripIt feed unreachable after retries, malformed ICS, missing/empty `tripit-url.txt`), surface a one-line note via `mcp__nanoclaw__send_message` carrying the script's stderr — do not swallow the failure — then proceed to Step 3. The file's mtime stays unchanged on failure, so Step 3's probe still re-checks staleness independently. On success, proceed to Step 3.

## Step 3 — Travel-schedule freshness probe with Gmail fallback

Do NOT alert on `travel-schedule.json` mtime alone. The escalation signal is a `stale` status **and** a matching TripIt forwarded-confirmation email from the Gmail check below, never mtime by itself.

```bash
python3 /home/node/.claude/skills/tessl__nightly-travel-sync/scripts/check-travel-freshness.py
```

Parse the JSON output and branch on `status` (the staleness threshold lives in `scripts/check-travel-freshness.py`):

- `"missing"` — file does not exist. Report via `mcp__nanoclaw__send_message`.
- `"fresh"` — silent. Skip to Step 4.
- `"stale"` — consult Gmail. Script output includes `gmail_query` (already buffered for the `after:` boundary), `subject_prefix`, and `mtime`. Pass `gmail_query` verbatim to the fetch script and pipe it into the filter — one command, no tool call:

```bash
set -o pipefail
python3 /home/node/.claude/skills/tessl__nightly-travel-sync/scripts/fetch-tripit-emails.py '<gmail_query>' \
  | python3 /home/node/.claude/skills/tessl__nightly-travel-sync/scripts/filter-tripit-bookings.py
```

`set -o pipefail` is required, not decoration: without it the filter's exit 0 masks the fetch's exit code and the truncation signal below is lost.

Do NOT fetch the mail yourself. The fetch script sanitizes every message inside the container before printing it, so raw email text never enters this session (`/workspace/group/nanoclaw-poison-defense.md`); an agent-driven Gmail call would put it there. Read only the filter's output.

Branch on the pipeline's exit code:

- **0** — the whole window was examined, so `count` is trustworthy. `0` → silent; `≥ 1` → report via `mcp__nanoclaw__send_message` with the matching subjects, the count, and the travel-schedule mtime.
- **3** — the fetch hit its message cap (its stderr carries `WINDOW_TRUNCATED`), so older mail in the window was never examined. **A `count` of 0 here does NOT mean "no booking found"** — report regardless of count via `mcp__nanoclaw__send_message`: say the confirmation check could only see the newest N emails since the schedule mtime, that a booking confirmation may be sitting behind the cap, and include any matches it did find. Note it will keep firing until the schedule refreshes, because the window widens with staleness.
- **1 or 2** — stdout is empty; a diagnostic is on stderr. Surface that stderr in a one-line note via `mcp__nanoclaw__send_message`. Exit 2 is operator-actionable config: the OneCLI gateway is not authenticating the Gmail call, this agent's tier is gated from Google by design, or the co-loaded `tessl__heartbeat` mount is missing. Exit 1 is a failed Gmail call; the next nightly fire retries.

Never read a non-zero exit as "no bookings found" — silence is correct only on exit 0. Proceed to Step 4 in every case.

## Step 4 — Rebuild travel-db.json from the schedule

```bash
python3 /home/node/.claude/skills/tessl__check-travel-bookings/scripts/build-travel-db.py
```

Produces `/workspace/group/travel-db.json`. The script lives in the sibling `check-travel-bookings` skill's dir. On non-zero exit, surface a one-line note via `mcp__nanoclaw__send_message`, emit `<internal>nightly-travel-sync exited step-4: build-nonzero</internal>` as your final turn text, and finish (Step 5 hard-fails on a missing DB, and the next cron re-runs from Step 1). Otherwise proceed to Step 5.

## Step 5 — Travel bookings check

```
Skill(skill: "tessl__check-travel-bookings")
```

Finds missing flights/hotels for upcoming trips. The inner skill reports gaps and is silent when all bookings are complete or snoozed.

Emit exactly one `<internal>` line so a silent-success watchdog can distinguish healthy quiet from broken-silently runs: `<internal>nightly-travel-sync ran <slot_key>: clean</internal>` when no step surfaced anything in Steps 1–5, or `<internal>nightly-travel-sync ran <slot_key>: surfaced</internal>` when at least one did. `<slot_key>` is today's UTC date in `YYYY-MM-DD` form. No user-facing output. Finish here.
