# jbaruch/nanoclaw-travel

[![tessl](https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.tessl.io%2Fv1%2Fbadges%2Fjbaruch%2Fnanoclaw-travel)](https://tessl.io/registry/jbaruch/nanoclaw-travel)

Actionable travel assistance for NanoClaw — byAir-powered flight notifications, travel-booking gap checks, and nightly TripIt sync. Replaces generic "21 minutes to departure" reminders with alerts that change behavior. Powered by [byAir](https://byairapp.com/mcp/) for flight data and Google Maps Distance Matrix for traffic-aware time-to-leave.

Per-chat overlay plugin. Install via NanoClaw's `containerConfig.additionalTiles` mechanism.

## Capabilities (V1.1 shipped)

1. **Time-to-leave** — traffic-aware push N hours before departure ("leave by 11:30, traffic is 45 min")
2. **Day-before sanity check** — diff against prior TripIt state; flag silent rebookings, seat changes, calendar conflicts
3. **Gate / delay / cancel push** — fires on actual change, not on cron schedule
4. **Connection risk** — alert when leg 1 delay threatens leg 2 transfer
5. **Inbound aircraft delay** — earliest possible signal (incoming aircraft delayed on previous leg, ~1h before gate-board flip)
6. **Arrival logistics** — ~15 min before landing: baggage carousel, Lyft estimate, lounge access if transit

## Installation

```
tessl install jbaruch/nanoclaw-travel
```

Add to a chat's overlay tile list via `update_group_config`:

```
additionalTiles: ["jbaruch/nanoclaw-travel"]
```

## Required environment

| Variable | Purpose | Where to get |
|----------|---------|--------------|
| `BYAIR_MCP_URL` | byAir streamable-HTTP MCP endpoint (includes API key) | https://byairapp.com/mcp/ — Pro subscription, personal MCP link |
| `GOOGLE_MAPS_API_KEY` | Distance Matrix API key for time-to-leave | https://console.cloud.google.com/apis/credentials |
| `COMPOSIO_API_KEY` | Composio project API key — Google Calendar actions (boarding-block reconciliation, drive-block create/remove, calendar fetches) | https://app.composio.dev — project settings |
| `COMPOSIO_USER_ID` | Composio user/entity the Google Calendar account is connected under; scopes every tool execution | https://app.composio.dev — connected accounts |

Optional:

| Variable | Purpose |
|----------|---------|
| `COMPOSIO_BASE_URL` | Override of the Composio REST endpoint; unset uses the public v3 backend |
| `TOMTOM_API_KEY` | Backup routing provider, used only when the Google Distance Matrix call fails; absent it, a Google failure propagates |

Store all required credentials in OneCLI vault. Never commit. See [.env.example](.env.example) for the contract; GitHub Actions secrets configuration link is in its file header.

## Rules

| Rule | Summary |
|------|---------|
| [flight-data-locality](rules/flight-data-locality.md) | byAir is the single upstream for flight data; AeroAPI / Flighty / airline-specific APIs forbidden |
| [operator-local-tz-phrasing](rules/operator-local-tz-phrasing.md) | Relative-date words ("today"/"tomorrow") in a surface are phrased against the operator's local date (via `read-current-tz.py`), not the container UTC clock; displayed airport clock times stay as-is |

## Skills

| Skill | Description |
|-------|-------------|
| [travel-core](skills/travel-core/SKILL.md) | Shared library bundle (not user-invocable): hosts the cross-skill `trip_origin` (TripIt-over-home position/anchor resolution) and `airport_lead` (clearance / post-arrival buffer policy) modules so flight-assist, drive-planner, and the drive engine import one source of truth. |
| [drive-engine](skills/drive-engine/SKILL.md) | Unified leg-based drive-block engine (#156), running in read-only **preview mode**. On a ~30-min precheck it assembles the airport drive legs the itinerary needs, diffs them against the primary calendar's current drive blocks, and logs the add/move/delete/replace plan without writing — the validation harness for unifying the flight-assist and drive-planner drive logic before the write path is enabled. |
| [flight-assist](skills/flight-assist/SKILL.md) | Action router: diagnose credentials, set home base, or compose a user-facing notification from a precheck wake event (delay, gate change, cancellation, boarding, time-to-leave, carousel, day-before, arrival logistics, tracked-flight add/remove) |
| [sync-tripit](skills/sync-tripit/SKILL.md) | Adaptive scheduler that fires the byAir → `active-flights.json` refresh on a precheck-gated 5-min cadence — responsive on flight days, idle between travel windows. Diagnostic-only LLM surface (the gate + sync happen in the precheck script) |
| [check-travel-bookings](skills/check-travel-bookings/SKILL.md) | Checks upcoming trips for missing bookings (flights, hotels, accommodation) by reading the nightly-built `travel-db.json`. Reports gaps for all upcoming trips — no date limit. Supports snooze state. Silent when all bookings are complete or snoozed. Use when the user asks about upcoming travel plans, itinerary completeness, missing reservations, or TripIt trip status. |
| [nightly-travel-sync](skills/nightly-travel-sync/SKILL.md) | Daily travel-data refresh bundle: TripIt → Reclaim timezone sync, refresh `travel-schedule.json` from the TripIt iCal feed with a two-tier Gmail freshness probe, rebuild `travel-db.json`, then run `check-travel-bookings`. Precheck-gated on `travel-db.json` freshness; surfaces failures and relies on the daily cron + freshness probe to recover. Self-contained writer of the data `check-travel-bookings` reads. |
| [drive-planner](skills/drive-planner/SKILL.md) | Action router for ground-transit drive planning. On a ~2h precheck sweep it classifies upcoming in-person meetings (`scan.py`), pre-routes each leg with live traffic, and creates a Free home → venue → home drive block for any that lacks one (create-first, idempotent per lombot #50), then notifies. A "skip `<id>`" reply removes the block and records a skip so it's never re-asked. The block IS the state — no local block store. |
| [drive-planner-recheck](skills/drive-planner-recheck/SKILL.md) | Traffic-growth watcher. On a ~15-min precheck poll it re-fetches its own drive blocks by API, re-routes each in-window arrival-anchored leg, and pushes a leave-earlier / leave-now alert when traffic grew past the threshold or the leave-by arrived. Each condition fires once (suppression patched onto the event). Re-derives its work from the blocks every poll, so a recheck can never be silently lost (lombot #48). |

## Skill scripts

The skill bundle includes executable scripts the agent invokes via the SKILL.md actions:

- `scripts/check-env.py` — verifies BYAIR_MCP_URL, GOOGLE_MAPS_API_KEY, COMPOSIO_API_KEY, and COMPOSIO_USER_ID are set
- `scripts/set-home-base.py` — persists home address to plugin config for time-to-leave queries
- `scripts/get-flight-state.py` — fetches a flight's last-known snapshot to enrich notifications
- `scripts/read-current-tz.py` — resolves the operator's `current_tz` from `tz_state` so surfaces phrase relative dates in the operator's local zone (see `operator-local-tz-phrasing` rule)

Plus scheduler-invoked scripts (not user-facing):

- `flight-assist/precheck.py` — runs every ~2 min, polls byAir per cadence ladder, emits wake events
- `sync-tripit/precheck.py` — runs every 5 min, adaptive-gated; delegates to `flight-assist/sync_tripit.py` only when a flight is imminent or the index is stale (see the `sync-tripit` skill for gate predicate + thresholds)
- `flight-assist/sync_tripit.py` — the byAir → state reconciliation invoked by the sync-tripit scheduler
- `nightly-travel-sync/precheck.py` — runs daily, gates the travel-data refresh on `travel-db.json` freshness (see the `nightly-travel-sync` skill + `precheck.py` for the cadence predicate)
- `nightly-travel-sync/scripts/refresh-travel-schedule.py`, `check-travel-freshness.py`, `filter-tripit-bookings.py` — the travel-source writers + freshness probe the bundle drives
- `drive-planner/precheck.py` — runs every ~2h, fetches the wide calendar window, classifies meetings, pre-routes legs, and wakes the agent only when a meeting needs a drive block; `apply.py` is the agent-invoked idempotent create / skip-remove
- `drive-planner/scan.py`, `fetch_events.py`, `block_props.py`, `recheck.py`, `skip_state.py`, `home_address.py` — the deterministic core (classifier, API calendar fetch, block codec, recheck gate, skip store, canonical-home reader)
- `drive-planner-recheck/precheck.py` — runs every ~15 min, re-fetches its own drive blocks, re-routes in-window legs, and wakes the agent only when a leave-earlier / leave-now alert must go out

## Status

- **V1.1** — adds connection-risk derivation (capability 4). The precheck post-loop walks per-trip state, projects leg-1 arrival vs leg-2 scheduled departure, and emits `connection_at_risk` events when the transfer window falls below `min_transfer_minutes` (configurable, default 45). State schema bumped to v2 with owner-side migration
- **V1** — flight-data-locality rule, full action-router SKILL.md, precheck orchestrator with cadence-gated byAir polling, stateful flight tracking, delta-driven wake rules (cancel, divert, gate, delay, inbound delay, boarding, carousel reveal), time-based wake gates (day-before, time-to-leave, arrival logistics), daily sync against `byair_list_trips`

See [CHANGELOG.md](CHANGELOG.md) for version history.
