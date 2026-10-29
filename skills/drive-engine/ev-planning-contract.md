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
An installed prefixed consumer never uses the source-clone fallback, even when
both native mounts are missing. Partial native mounts never fall back either. Complete named
regular transport targets and every component from the fixed provider root
to them must exist without symlinks before execution. Pass literals in argv;
never interpolate selectors into shell source. Both pipes and waits are bounded.
Errors print fixed diagnostics, retain legacy timing and never try direct HTTP,
copied provider code or another identity. The host rechecks protected enrollment,
roles, current dynamic non-archived fleet and all current provider contracts.

Travel's trusted base manifest identifies `jbaruch/nanoclaw-travel`. External
provider identity was separately verified through the normal authenticated
declared Tessl install of `jbaruch/nanoclaw-tesla@0.3.0`. All 35 installed regular
runtime/authority files matched published source
`4fa72e5cb0fbd30372bafe3db098c989484b1902` byte-for-byte, including its
[provider manifest](https://github.com/jbaruch/nanoclaw-tesla/blob/4fa72e5cb0fbd30372bafe3db098c989484b1902/.tessl-plugin/plugin.json).
The provider repository's actual origin identity is `jbaruch/nanoclaw-tesla`.
Its complete regular rule blobs were read from that registry install:

- [calendar-charge-policy](https://github.com/jbaruch/nanoclaw-tesla/blob/4fa72e5cb0fbd30372bafe3db098c989484b1902/rules/calendar-charge-policy.md)
  declares the native charge consumer and its full contract read.
- [nanoclaw-skill-root](https://github.com/jbaruch/nanoclaw-tesla/blob/4fa72e5cb0fbd30372bafe3db098c989484b1902/rules/nanoclaw-skill-root.md)
  enumerates the four authored skills, co-shipped `tessie-*.py` targets and their
  docstring input/output contracts.
- [provider-identity](https://github.com/jbaruch/nanoclaw-tesla/blob/4fa72e5cb0fbd30372bafe3db098c989484b1902/rules/provider-identity.md)
  binds that manifest, repository and registry namespace.

The installed complete `tessie-calendar-charge-consumer-v1` reference was also
read before integration. This source-review provenance is not a runtime version
pin; the declared dependency remains managed `latest`. Preserve these target,
identity and contract checks after upgrades and hold on incompatible providers.
Read the installed Host `tessie-calendar-charge.md` and `tessie-host-authority.md`
before provider integration. Lookup and delegation execute inside the real
`tesla_planning.py` file and consume structured JSON from real provider scripts.
Ordinary script requirements apply; no inline-bootstrap or non-JSON stdout
exception is required. Provider metadata grants no runtime authority.

## Inputs and unavailable behavior

Capacity and weighted driving consumption come from that reusable API. They are
not measured highway-only consumption. Explicit cold/highway planning allowances
remain separate. Rated range, station power and charger availability are never
substitutes. Stop count is a minimum assuming full usable recharges; configured
duration is an allowance, not a charger itinerary or charging-speed promise.

The planner carries remaining energy through chronological admitted legs of
the same exact native vehicle. It never reads a fresh return-leg forecast as a
full battery. Missing distance, unknown forecast, route/vehicle ambiguity,
invalid measurements or newly introduced overlap retains the affected dependent
legacy blocks. An unresolved middle drive fences later possible/default vehicle
uses; only independent fresh exact active-route arrival energy grounds a later
chain again. Held future route failures also enter this fence with their
source timing and possible selector. An unknown earliest start conservatively
fences from the sweep clock. Exact held keys that are still planned by another
readable source do not add a gap. Out-of-horizon legs remain unchanged without suppressing nearer
drives. Disconnected endpoints never replenish the same vehicle's energy. A
return with no selected, still-accounted outbound holds that return and later
dependent vehicle legs; a fresh exact active route's measured arrival energy
may supply independent remaining-route evidence. Configured missing/ambiguous data emits an
actionable stderr diagnostic; it does not silently become a successful EV plan.
When a fetched owned block already displays an EV allowance, unavailable inputs
use the existing non-announcing `HeldLeg` path and retain that block. A new drive
without EV data keeps the legacy plan; no unknown forecast shortens a retained
allowance.
Short drives with sufficient energy retain their original objects and output.
The forecast is reevaluated at an earlier calculated departure within its finite
read-only bound; unstable forecasts retain legacy timing.

The existing Calendar description string renders an `EV planning:` human line.
Reconciliation compares that rendered presentation independently of ordinary
traffic jitter, including adding/removing the minimum configured allowance.
Identical presentation keeps the existing traffic tolerance and remains a no-op.
The description grants no vehicle authority. Private `dengine_` schema 1 and its
fields/meaning remain unchanged. `dengine_b` remains routed driving seconds;
charging/navigation span changes live only in native Calendar times and human
presentation. Reconcile reads those native times separately for EV comparison
and material-update notices; no new persisted machine field is introduced.
Native start/end also govern retained-block overlap and post-arrival checks.
A longer depart-after return means arriving later, with its departure fixed;
material notices preserve this polarity separately from earlier outbound leave-by.
Grounded destination energy within the owner's documented bound is clamped to
usable capacity rather than silently discarded above the pack's capacity.

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
raw vehicle state enter the notification. Arrival charge remains internal; the
Calendar description and notification use only a generic navigation-derived note. Unknown/stale/other destinations leave
the ordinary measured plan unchanged. No wake, navigation command or location
trial is part of this feature.

## Grounded preferences

Read actual owner requests and native selectors before configuring. Defaults and
bounds live in `PlanningOptions` and `validate_ev_preferences`. Supply one complete
JSON object to `configure_ev.py` as a literal argv value. It prints schema-1
`recorded` and preserves all other config fields; it calls no provider or calendar.

The complete nested schema, numeric/string/list bounds, evidence fields and
reader/writer preservation behavior belong to the configuration owner:

```text
skills/flight-assist/state-schema.md
```

Read that owner contract before preference intake. Mapping conflicts and unknown
named drivers hold EV adjustment rather than choosing a different default vehicle.
Absent preferences use only the provider's supported unique/default resolution;
ambiguous fleets remain unchanged.

`config.json` stays under its existing flight-assist owner. Version 7→8 preserves
every existing field and only adds optional input support. Config, flight and
active-index writer/readers co-ship; no cross-plugin writer flip occurs. Unknown
root versions preserve the existing owner refusal. The shared reader carries
optional EV preferences unchanged; non-EV flight consumers do not interpret them.
Unrelated config updates may carry an existing opaque EV value unchanged, with
an actionable diagnostic, including non-finite values accepted by the existing
JSON reader. They cannot introduce or alter an invalid EV record.
The drive sweep and preference intake validate EV input strictly before using it.
Malformed/future EV input holds drive apply or preference replacement without
interrupting flight-alert polling or downgrading the nested record.

## Delivery and acceptance

Hold this consumer's merge/publication until the actual compatible Tesla13
producer/transport source and Host authority are available under their release
contract. A synthetic test provider never satisfies that hold. Root owns installed
byte comparison, real cached fleet/route/forecast validation and normal shadow /
ordinary sweep acceptance. No vehicle control, real provider trial or native
deployment is authorized by these source fixtures. Keep issue 339 open until
required native acceptance is recorded.
