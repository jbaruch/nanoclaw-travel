# jbaruch/nanoclaw-flight-assist

[![tessl](https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.tessl.io%2Fv1%2Fbadges%2Fjbaruch%2Fnanoclaw-flight-assist)](https://tessl.io/registry/jbaruch/nanoclaw-flight-assist)

Actionable flight notifications for NanoClaw. Replaces generic "21 minutes to departure" reminders with alerts that change behavior. Powered by [byAir](https://byairapp.com/mcp/) for flight data and Google Maps Distance Matrix for traffic-aware time-to-leave.

Per-chat overlay tile. Install via NanoClaw's `containerConfig.additionalTiles` mechanism.

## Capabilities (target — V1 in progress)

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
| [flight-assist](skills/flight-assist/SKILL.md) | Sequential workflow: diagnose env credentials. Will evolve into an action router as polling and event composition land |

## Status

- **v0.1.0 (initial scaffold)** — rule + env-diagnostic skill; remaining V1 capabilities land in subsequent PRs

See [CHANGELOG.md](CHANGELOG.md) for version history.
