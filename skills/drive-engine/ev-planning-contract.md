# EV drive-time planning

## Owners and supported dependency

`ev_charging.py` owns pure stop-time/cumulative-energy planning. `reconcile_sweep.py`
applies it to the complete meeting/airport/lodging desired set before the existing
single reconcile. `flight-assist/state.py` owns config schema 8 and additive
`ev_planning`; `ev_preferences.py` validates that nested schema 1. `configure_ev.py`
is its intake. No new vehicle state, access policy or command store exists.

The declared `jbaruch/nanoclaw-tesla` dependency supplies two executable targets:

- `skills/tessie-ops/scripts/tessie-read.py state --include-location [--vehicle LITERAL]`:
  native owner-only cached state, resolved canonical vehicle alias and optional
  active-route fields. Ambiguous default selection refuses. Never wake a car.
- `skills/tessie-charge/scripts/tessie-charge.py estimate --vehicle LITERAL
  --distance-km NUMBER --departure ZONED_ISO`: schema-1 conditional measured-energy
  forecast. It grants no control. Never invoke its `poll` operation here.

`tesla_planning.py` resolves only the fixed native `tessl__tessie-ops` and
`tessl__tessie-charge` skill directories. A source clone may use only its declared
`.tessl/plugins/jbaruch/nanoclaw-tesla` installation with matching manifest identity.
Partial native mounts never fall back to another installation. Complete named
regular transport targets must exist before execution. Pass literals in argv;
never interpolate selectors into shell source. Both pipes and waits are bounded.
Errors print fixed diagnostics, retain legacy timing and never try direct HTTP,
copied provider code or another identity. The host rechecks protected enrollment,
roles, current dynamic non-archived fleet and all current provider contracts.

Authoring identity is the trusted Tesla manifest `jbaruch/nanoclaw-tesla` in
`jbaruch/nanoclaw-tesla`; its declared `calendar-charge-policy`,
`nanoclaw-skill-root` and `provider-identity` rules own provider lookup/transport.
Read the installed Host `tessie-calendar-charge.md` and `tessie-host-authority.md`
before provider integration. This named scope is the Scoped Plugin Contracts
exception in coding-policy's script-output contract. It grants no runtime authority.

## Inputs and unavailable behavior

Capacity and weighted driving consumption come from that reusable API. They are
not measured highway-only consumption. Explicit cold/highway planning allowances
remain separate. Rated range, station power and charger availability are never
substitutes. Stop count is a minimum assuming full usable recharges; configured
duration is an allowance, not a charger itinerary or charging-speed promise.

The planner carries remaining energy through connected chronological legs of
the same exact native vehicle. It never reads a fresh return-leg forecast as a
full battery. Missing distance, unknown forecast, route/vehicle ambiguity,
disconnected legs, invalid measurements or newly introduced overlap retains that
vehicle's original legacy blocks. Configured missing/ambiguous data emits an
actionable stderr diagnostic; it does not silently become a successful EV plan.
Short drives with sufficient energy retain their original objects and output.
The forecast is reevaluated at an earlier calculated departure within its finite
read-only bound; unstable forecasts retain legacy timing.

Only a positive destination-charge input adds intervening energy. A hotel,
conference, multi-day gap or nearby charger does not imply charging. Bind evidence
to the exact selected vehicle, literal destination, zoned availability time and
grounded energy amount. Read source references before intake. Never invent the
amount from rated range or an unconfirmed overnight target. With such evidence,
outbound and return are two one-way energy checks; without it they remain
cumulative even across days.

On the day, the owner-only cached active route may replace the duration/arrival
energy only for the exact canonical vehicle and whole normalized destination,
within the freshness/departure bounds in `Navigation.matches`. No coordinates or
raw vehicle state enter the notification. Unknown/stale/other destinations leave
the ordinary measured plan unchanged. No wake, navigation command or location
trial is part of this feature.

## Grounded preferences

Read actual owner requests and native selectors before configuring. Defaults and
bounds live in `PlanningOptions` and `validate_ev_preferences`. Supply one complete
JSON object to `configure_ev.py` as a literal argv value. It prints schema-1
`recorded` and preserves all other config fields; it calls no provider or calendar.

Required nested schema-1 fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer 1 |
| `stop_minutes`, `reserve_percent` | Integers, validated by the owner |
| `cold_allowance_percent`, `highway_allowance_percent` | Finite numeric planning allowances |
| `default_vehicle` | Null or exact current native alias/VIN |
| `event_vehicles` | Native event/drive identity to exact alias/VIN |
| `person_vehicles` | Grounded driver label to exact alias/VIN |
| `event_drivers` | Native event/drive identity to confirmed driver label; never infer from attendees |
| `destination_charges` | Evidence rows below, or empty array |

Each evidence row has exactly `schema_version:1`, `vehicle`, `destination`,
`available_at` (zoned ISO instant), finite positive `energy_kwh` and nonempty
`source` (owner message or native observation reference). Duplicate same
vehicle/place/instant evidence refuses. Mapping conflicts and unknown named
drivers hold EV adjustment rather than choosing a different default vehicle.
Absent preferences use only the provider's supported unique/default resolution;
ambiguous fleets remain unchanged.

`config.json` stays under its existing flight-assist owner. Version 7→8 preserves
every existing field and only adds optional input support. Config, flight and
active-index writer/readers co-ship; no cross-plugin writer flip occurs. Unknown
versions preserve the existing owner refusal. Present malformed EV config refuses
before apply; no silent reset, rewrite or substitution occurs.

## Delivery and acceptance

Hold this consumer's merge/publication until the actual compatible Tesla13
producer/transport source and Host authority are available under their release
contract. A synthetic test provider never satisfies that hold. Root owns installed
byte comparison, real cached fleet/route/forecast validation and normal shadow /
ordinary sweep acceptance. No vehicle control, real provider trial or native
deployment is authorized by these source fixtures. Keep issue 339 open until
required native acceptance is recorded.
