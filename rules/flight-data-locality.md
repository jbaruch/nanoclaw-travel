---
alwaysApply: true
---

# Flight Data Locality

## Single Upstream

- byAir is the single source of truth for flight status, gate, delay, baggage carousel, and inbound aircraft chain
- Do not introduce a second flight-data API (AeroAPI, Flighty, FlightAware direct, airline-specific APIs) — duplicate signals from different upstreams diverge and breed reconciliation bugs
- If byAir is missing a field, the answer is either (a) report it upstream, or (b) decide the field is out of scope. Never (c) add a second upstream

## Why

- byAir pre-computes phase logic (`computed_status`, `computed_phase_progress`, `computed_phase_risk`, `computed_phase_overdue`) and inbound-aircraft prediction (`inbound.predicted_delay`). Mixing a raw-status API forces a translation layer between two semantically-different models
- Cost: the byAir Pro subscription covers every operational field this tile needs. A second upstream adds a separate budget, a separate key, and a separate rate-limit posture for marginal data
- Reconciliation: when byAir reports "boarding" and AeroAPI reports "scheduled", which wins? The right answer is "the question doesn't arise" — one upstream, one truth

## Exceptions

- **Maps / traffic** is not flight data — it's a separate axis (origin location, drive time). Google Maps Distance Matrix is allowed and expected for the time-to-leave capability
- **Calendar / TripIt** is the source-of-record for which flights exist (and which hotels / seats / bookings attach to them). byAir is the source-of-record for what those flights are doing right now. The two don't overlap

## How to Apply

- A new flight-data integration request starts with "does byAir already expose this?" — most of the time the answer is yes
- A PR that adds a second flight upstream is `REQUEST_CHANGES` by default. The PR description must explicitly call out the byAir gap that justifies the addition
- Surfaces that already exist (precheck script, MCP client, state files) stay byAir-only — no boy-scout-rule "while I'm in here" cross-pollination from other sources
