---
alwaysApply: true
---

# Flight Data Locality

## Single Upstream

- byAir is the source of truth for flight status, gate, delay, baggage carousel, and inbound aircraft chain
- A second flight-data API is forbidden. AeroAPI, Flighty, FlightAware direct, and airline-specific APIs do not enter the plugin
- Missing fields are reported upstream or descoped. A second API is not the remedy

## Out of Scope

- Maps and traffic data live on a separate axis. Google Maps Distance Matrix is the source for time-to-leave
- Calendar and TripIt are the source-of-record for which flights exist
- byAir is the source-of-record for what those flights are doing now

## How to Apply

- New flight-data integration request starts with: "does byAir already expose this?"
- A PR adding a second flight upstream is `REQUEST_CHANGES` by default
- The PR description must name the specific byAir gap that justifies the addition
- Existing surfaces (precheck script, MCP client, state files) stay byAir-only
