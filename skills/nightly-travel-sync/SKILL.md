---
name: nightly-travel-sync
description: "Travel-data refresh bundle: TripIt → Reclaim timezone sync, refresh travel-schedule.json from the TripIt iCal feed with a two-tier Gmail freshness probe, rebuild travel-db.json, then check upcoming trips for booking gaps. Runs daily; precheck-gated on travel-db.json freshness. Triggers: 'sync trips', 'sync travel', 'update travel data', 'pull trip info', 'refresh travel schedule', 'rebuild travel db', 'check my bookings'."
cadence: "0 6 * * * (TZ=local)"
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
- `"stale"` — consult Gmail. Script output includes `gmail_query` (already buffered for the `after:` boundary), `subject_prefix`, and `mtime`. Discover the Gmail fetch tool first — `COMPOSIO_SEARCH_TOOLS(query="gmail fetch emails")` returns `GMAIL_FETCH_EMAILS`; do not hardcode the tool name. Call it with `query=<gmail_query>`, build a JSON array (`subject` minimum; ideally `id`/`from`/`date`), and pipe to the filter:

```bash
python3 /home/node/.claude/skills/tessl__nightly-travel-sync/scripts/filter-tripit-bookings.py < /tmp/tripit-emails.json
```

Decision on `count`: `0` → silent; `≥ 1` → report via `mcp__nanoclaw__send_message` with the matching subjects, the count, and the travel-schedule mtime. Proceed to Step 4 either way.

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

Emit exactly one `<internal>` line so a silent-success watchdog can distinguish healthy quiet from broken-silently runs: `<internal>nightly-travel-sync ran: clean</internal>` when no step surfaced anything in Steps 1–5, or `<internal>nightly-travel-sync ran: surfaced</internal>` when at least one did. No user-facing output. Finish here.
