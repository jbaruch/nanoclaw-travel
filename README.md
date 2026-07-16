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

Google Calendar access (boarding-block reconciliation, drive-block
create/remove, calendar fetches) needs **no variable**: it calls the native
Calendar REST API, and OneCLI's gateway injects the OAuth Bearer on the wire
(nanoclaw#638). The container holds no Google credential. The retired
`COMPOSIO_API_KEY` / `COMPOSIO_USER_ID` pair can be deleted from the vault.

Optional:

| Variable | Purpose |
|----------|---------|
| `TOMTOM_API_KEY` | Backup routing provider, used only when the Google Distance Matrix call fails; absent it, a Google failure propagates |

Store all required credentials in OneCLI vault. Never commit. See [.env.example](.env.example) for the contract; GitHub Actions secrets configuration link is in its file header.

## Co-loaded plugin dependency

`nightly-travel-sync`'s Gmail freshness fallback (`scripts/fetch-tripit-emails.py`) imports four shared Gmail helpers — `google-rest.py`, `gmail-ops.py`, `gmail-message.py`, `sanitize-email-body.py` — from **`jbaruch/nanoclaw-admin`**'s heartbeat skill, over the co-loaded `tessl__heartbeat` mount. So this plugin's `additionalTiles` must also carry `jbaruch/nanoclaw-admin` for that one branch to run; it fails closed with an actionable message otherwise, and no other capability here depends on it.

Gmail is not this plugin's domain, so it depends on the one tested copy of the RFC822 MIME parser and the poison sanitizer rather than re-implementing them (`nanoclaw-orders` consumes them the same way). Calendar is different — this plugin owns its per-service clients, so `google_calendar_client.py` stays self-contained here.

## Rules

| Rule | Summary |
|------|---------|
| [flight-data-locality](rules/flight-data-locality.md) | byAir is the single upstream for flight data; AeroAPI / Flighty / airline-specific APIs forbidden |
| [operator-local-tz-phrasing](rules/operator-local-tz-phrasing.md) | Relative-date words ("today"/"tomorrow") in a surface are phrased against the operator's local date (via `read-current-tz.py`), not the container UTC clock; displayed airport clock times stay as-is |

## Skills

| Skill | Description |
|-------|-------------|
| [travel-core](skills/travel-core/SKILL.md) | Shared library bundle (not user-invocable): hosts the cross-skill `trip_origin` (TripIt-over-home position/anchor resolution) and `airport_lead` (clearance / post-arrival buffer policy) modules so flight-assist, drive-planner, and the drive engine import one source of truth. |
| [drive-engine](skills/drive-engine/SKILL.md) | Unified leg-based drive-block engine (#156). On a ~30-min sweep it plans airport drives from the byAir itinerary and meeting drives from the calendar, diffs both against the primary calendar, and **applies** the changes — creating / updating / deleting its own blocks. Suppresses drives that can't be made (connection airports, home meetings while travelling), renders in local time, and leaves legacy blocks for the operator. Notifies the operator ONLY on a new meeting drive (which they can skip by replying "skip", enumerated by local index) or a material (≥10%) drive-time change ("leave N min sooner/later"); removes, airport-drive adds, and routine re-times apply silently. Replaces the flight-assist airport-drive pass and drive-planner. |
| [flight-assist](skills/flight-assist/SKILL.md) | Action router: diagnose credentials, set home base, or compose a user-facing notification from a precheck wake event (delay, gate change, cancellation, boarding, time-to-leave, carousel, day-before, arrival logistics, tracked-flight add/remove) |
| [sync-tripit](skills/sync-tripit/SKILL.md) | Adaptive scheduler that fires the byAir → `active-flights.json` refresh on a precheck-gated 5-min cadence — responsive on flight days, idle between travel windows. Diagnostic-only LLM surface (the gate + sync happen in the precheck script) |
| [check-travel-bookings](skills/check-travel-bookings/SKILL.md) | Checks upcoming trips for missing bookings (flights, hotels, accommodation) by reading the nightly-built `travel-db.json`. Reports gaps for all upcoming trips — no date limit. Supports snooze state. Silent when all bookings are complete or snoozed. Use when the user asks about upcoming travel plans, itinerary completeness, missing reservations, or TripIt trip status. |
| [nightly-travel-sync](skills/nightly-travel-sync/SKILL.md) | Daily travel-data refresh bundle: TripIt → Reclaim timezone sync, refresh `travel-schedule.json` from the TripIt iCal feed with a two-tier Gmail freshness probe, rebuild `travel-db.json`, then run `check-travel-bookings`. Precheck-gated on `travel-db.json` freshness; surfaces failures and relies on the daily cron + freshness probe to recover. Self-contained writer of the data `check-travel-bookings` reads. |
| [drive-planner](skills/drive-planner/SKILL.md) | **RETIRED (#156)** — superseded by drive-engine, which now plans and writes meeting drives. Non-invocable, no schedule; kept only as a library so drive-engine can import its meeting-detection code (`scan.py`, `fetch_events.py`, `skip_state.py`). |
| [drive-planner-recheck](skills/drive-planner-recheck/SKILL.md) | **RETIRED (#156)** — superseded by drive-engine. Non-invocable, no schedule; no longer polls or alerts. |

## Skill scripts

The skill bundle includes executable scripts the agent invokes via the SKILL.md actions:

- `scripts/check-env.py` — verifies BYAIR_MCP_URL and GOOGLE_MAPS_API_KEY are set (calendar access has no env var to check — see above)
- `scripts/set-home-base.py` — persists home address to plugin config for time-to-leave queries
- `scripts/get-flight-state.py` — fetches a flight's last-known snapshot to enrich notifications
- `scripts/read-current-tz.py` — resolves the operator's `current_tz` from `tz_state` so surfaces phrase relative dates in the operator's local zone (see `operator-local-tz-phrasing` rule)

Plus scheduler-invoked scripts (not user-facing):

- `flight-assist/precheck.py` — runs every ~2 min, polls byAir per cadence ladder, emits wake events
- `sync-tripit/precheck.py` — runs every 5 min, adaptive-gated; delegates to `flight-assist/sync_tripit.py` only when a flight is imminent or the index is stale (see the `sync-tripit` skill for gate predicate + thresholds)
- `flight-assist/sync_tripit.py` — the byAir → state reconciliation invoked by the sync-tripit scheduler
- `nightly-travel-sync/precheck.py` — runs daily, gates the travel-data refresh on `travel-db.json` freshness (see the `nightly-travel-sync` skill + `precheck.py` for the cadence predicate)
- `nightly-travel-sync/scripts/refresh-travel-schedule.py`, `check-travel-freshness.py`, `fetch-tripit-emails.py`, `filter-tripit-bookings.py` — the travel-source writers + the freshness probe's Gmail fallback (fetch sanitizes in-container, filter matches the TripIt confirmation prefix)
- `drive-engine/reconcile_sweep.py` — runs every ~30 min, plans airport + meeting drives, reconciles against the primary calendar, and applies the changes (create / update / delete of its own blocks)
- `drive-planner/scan.py`, `fetch_events.py`, `skip_state.py` — retired to a library: the meeting-detection core (classifier, calendar fetch, skip store) imported by drive-engine's sweep

## Status

- **V1.1** — adds connection-risk derivation (capability 4). The precheck post-loop walks per-trip state, projects leg-1 arrival vs leg-2 scheduled departure, and emits `connection_at_risk` events when the transfer window falls below `min_transfer_minutes` (configurable, default 45). State schema bumped to v2 with owner-side migration
- **V1** — flight-data-locality rule, full action-router SKILL.md, precheck orchestrator with cadence-gated byAir polling, stateful flight tracking, delta-driven wake rules (cancel, divert, gate, delay, inbound delay, boarding, carousel reveal), time-based wake gates (day-before, time-to-leave, arrival logistics), daily sync against `byair_list_trips`

See [CHANGELOG.md](CHANGELOG.md) for version history.
