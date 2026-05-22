# jbaruch/nanoclaw-flight-assist

[![tessl](https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.tessl.io%2Fv1%2Fbadges%2Fjbaruch%2Fnanoclaw-flight-assist)](https://tessl.io/registry/jbaruch/nanoclaw-flight-assist)

Actionable flight notifications for NanoClaw. Replaces generic "21 minutes to departure" reminders with alerts that change behavior. Powered by [byAir](https://byairapp.com/mcp/) for flight data and Google Maps Distance Matrix for traffic-aware time-to-leave.

Per-chat overlay tile. Install via NanoClaw's `containerConfig.additionalTiles` mechanism.

## Capabilities (V1.1 shipped)

1. **Time-to-leave** — traffic-aware push N hours before departure ("leave by 11:30, traffic is 45 min")
2. **Day-before sanity check** — diff against prior TripIt state; flag silent rebookings, seat changes, calendar conflicts
3. **Gate / delay / cancel push** — fires on actual change, not on cron schedule
4. **Connection risk** — alert when leg 1 delay threatens leg 2 transfer
5. **Inbound aircraft delay** — earliest possible signal (incoming aircraft delayed on previous leg, ~1h before gate-board flip)
6. **Arrival logistics** — ~15 min before landing: baggage carousel, Lyft estimate, lounge access if transit

## Installation

```
tessl install jbaruch/nanoclaw-flight-assist
```

Add to a chat's overlay tile list via `update_group_config`:

```
additionalTiles: ["jbaruch/nanoclaw-flight-assist"]
```

## Required environment

| Variable | Purpose | Where to get |
|----------|---------|--------------|
| `BYAIR_MCP_URL` | byAir streamable-HTTP MCP endpoint (includes API key) | https://byairapp.com/mcp/ — Pro subscription, personal MCP link |
| `GOOGLE_MAPS_API_KEY` | Distance Matrix API key for time-to-leave | https://console.cloud.google.com/apis/credentials |

Store both in OneCLI vault. Never commit. See [.env.example](.env.example) for the contract; GitHub Actions secrets configuration link is in its file header.

## Rules

| Rule | Summary |
|------|---------|
| [flight-data-locality](rules/flight-data-locality.md) | byAir is the single upstream for flight data; AeroAPI / Flighty / airline-specific APIs forbidden |

## Skills

| Skill | Description |
|-------|-------------|
| [flight-assist](skills/flight-assist/SKILL.md) | Action router: diagnose credentials, set home base, or compose a user-facing notification from a precheck wake event (delay, gate change, cancellation, boarding, time-to-leave, carousel, day-before, arrival logistics, tracked-flight add/remove) |
| [sync-tripit](skills/sync-tripit/SKILL.md) | Adaptive scheduler that fires the byAir → `active-flights.json` refresh on a precheck-gated 5-min cadence — responsive on flight days, idle between travel windows. Diagnostic-only LLM surface (the gate + sync happen in the precheck script) |

## Skill scripts

The skill bundle includes three executable scripts the agent invokes via the SKILL.md actions:

- `scripts/check-env.py` — verifies BYAIR_MCP_URL + GOOGLE_MAPS_API_KEY are set
- `scripts/set-home-base.py` — persists home address to tile config for time-to-leave queries
- `scripts/get-flight-state.py` — fetches a flight's last-known snapshot to enrich notifications

Plus scheduler-invoked scripts (not user-facing):

- `flight-assist/precheck.py` — runs every ~2 min, polls byAir per cadence ladder, emits wake events
- `sync-tripit/precheck.py` — runs every 5 min, adaptive-gated; delegates to `flight-assist/sync_tripit.py` only when a flight is imminent or the index is stale (see the `sync-tripit` skill for gate predicate + thresholds)
- `flight-assist/sync_tripit.py` — the byAir → state reconciliation invoked by the sync-tripit scheduler

## Status

- **V1.1** — adds connection-risk derivation (capability 4). The precheck post-loop walks per-trip state, projects leg-1 arrival vs leg-2 scheduled departure, and emits `connection_at_risk` events when the transfer window falls below `min_transfer_minutes` (configurable, default 45). State schema bumped to v2 with owner-side migration
- **V1** — flight-data-locality rule, full action-router SKILL.md, precheck orchestrator with cadence-gated byAir polling, stateful flight tracking, delta-driven wake rules (cancel, divert, gate, delay, inbound delay, boarding, carousel reveal), time-based wake gates (day-before, time-to-leave, arrival logistics), daily sync against `byair_list_trips`

See [CHANGELOG.md](CHANGELOG.md) for version history.
