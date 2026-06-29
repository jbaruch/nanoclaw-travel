# Precheck Wake Event Payloads

Reference for the `data.events` payload the precheck script emits on `wake_agent: true`. Each event is `{"flight_id": int, "event": {...}}`. Consumed by `SKILL.md` Step 3 to compose user-facing notifications.

## Event Reasons

### Delta-driven (from `wake_rules.py`)

#### `cancelled`

```json
{"reason": "cancelled"}
```

Fires on `computed_status` transition into `cancelled`, OR on first cycle if the flight is already cancelled.

#### `diverted`

```json
{"reason": "diverted"}
```

Same trigger as `cancelled` but for `diverted` status.

#### `gate_change`

```json
{"reason": "gate_change", "side": "dep" | "arr", "from": "B25", "to": "B7"}
```

`to` may be `null` (gate removed). `from` is always non-null (first publication of a gate doesn't fire).

#### `delay`

```json
{"reason": "delay", "delay_minutes": 22, "new_dep_time": "2026-05-17T13:00:00-07:00"}
{"reason": "delay", "delay_minutes": 31, "new_dep_time": "2026-06-04T21:11:00+02:00", "schedule_slip": true}
```

`delay_minutes` is the size of the delay in minutes (positive = later than before, negative = moved earlier); `new_dep_time` is the departure to show. The optional `schedule_slip: true` flag marks a delay already present on the first poll; compose it identically to a plain delay. Firing thresholds and the slip-vs-schedule rule live in `wake_rules.py` (`detect_wake_events`; constants in the module docstring).

#### `inbound_delay_predicted`

```json
{"reason": "inbound_delay_predicted", "delay_minutes": 35, "predicted_time": "2026-05-17T13:00:00-07:00"}
```

`delay_minutes` is the predicted inbound-aircraft delay; `predicted_time` is the new estimated departure. Firing threshold and dedupe live in `wake_rules.py` (`detect_wake_events`).

#### `inbound_delay_retracted`

```json
{"reason": "inbound_delay_retracted", "prev_delay_minutes": 95, "new_delay_minutes": null}
```

`prev_delay_minutes` is the delay last surfaced to the user; `new_delay_minutes` is the current prediction (`null` when retracted entirely). Send the all-clear even if an earlier surface escalated to "rebook now" — without it, silence reads as still-delayed. Firing conditions live in `wake_rules.py` (`detect_wake_events`).

#### `boarding_started`

```json
{"reason": "boarding_started"}
```

Fires on transition into actual boarding, gated on the real-boarding signal rather than the `computed_status` label alone — byAir labels the phase `boarding` before boarding starts. First-cycle "already boarding" does not fire. The `phase_markers.boarding_fired` flag is reserved for a future boarding-prep notification and does not gate this event in v0.1. Firing conditions and the real-boarding predicate live in `wake_rules.py` (`detect_wake_events`, `is_real_boarding`).

#### `carousel_revealed`

```json
{"reason": "carousel_revealed", "baggage": "CLM1"}
```

Fires when `baggage` transitions `null` → populated.

### Cross-flight (from `connection_risk.py`)

#### `connection_at_risk`

```json
{
  "reason": "connection_at_risk",
  "leg1_code": "AA100",
  "leg2_code": "AA200",
  "leg1_flight_id": 12345,
  "connecting_airport_id": 28,
  "transfer_minutes_remaining": 32,
  "missed_connection": false,
  "scheduled_layover_minutes": 70,
  "min_transfer_minutes": 45,
  "projected_leg1_arr_time": "2026-05-17T13:28:00-07:00",
  "scheduled_leg2_dep_time": "2026-05-17T14:00:00-07:00"
}
```

Fires when the projected transfer window between leg-1 arrival and leg-2 departure falls below `min_transfer_minutes` (configurable via `config.json:min_transfer_minutes`, default 45). The event is keyed to leg-2's `flight_id` (the at-risk downstream leg) so the once-per-flight phase marker (`connection_at_risk_fired`) survives leg-1 landing.

`missed_connection` is `true` when `transfer_minutes_remaining` is at or below zero — i.e., leg-1 is projected to arrive at or after leg-2 has already departed, so the connection is structurally lost. SKILL.md branches on the flag to render a "rebook required" message instead of a sub-zero "boards in N min" string.

Suppression rules:

- Skips when leg-1 status is `landed` (outcome observable), `cancelled`, or `diverted` (a more specific alert path fires)
- Skips when leg-1 scheduled departure is more than 24h away (early projections are too speculative)
- Skips when `connection_at_risk_fired` is already `true` on the leg-2 record (once-per-flight)

### Time-based (from `phase_markers.py`)

#### `day_before`

```json
{"reason": "day_before", "scheduled_dep_time": "2026-05-18T17:00:00+00:00", "hours_until_dep": 24}
```

Fires once per flight at `T - 24h`.

#### `time_to_leave`

```json
{"reason": "time_to_leave", "leave_by": "2026-05-18T16:15:00+00:00", "travel_time_minutes": 30, "scheduled_dep_time": "2026-05-18T17:00:00+00:00"}
```

Fires when `now + travel_time + 15min buffer ≥ scheduled_dep_time`. `travel_time_minutes` is the in-traffic estimate from Google Maps Distance Matrix. Suppressed once the snapshot shows the flight boarding or departed — the leave-by alert is moot by then (#102).

#### `gate_assignment`

```json
{"reason": "gate_assignment", "dep_gate": "E16", "dep_terminal": "2"}
```

One-time departure gate + terminal readout, fired the first cycle a gate exists inside the pre-boarding window (`scheduled_dep − boarding_lead − 1h`). `dep_terminal` is null when byAir hasn't published a terminal. Gate info before the window is recorded to state silently; gate moves after this readout surface as `gate_change` (#103).

#### `arrival_logistics`

```json
{"reason": "arrival_logistics", "scheduled_arr_time": "2026-05-18T20:00:00+00:00", "minutes_until_arr": 15}
```

Fires once per flight at `T-arr − 15min`.

### Derived (precheck-side)

#### `removed_upstream`

```json
{"reason": "removed_upstream"}
```

Fires when byAir returns 404 for a previously-tracked flight. The agent should surface this to the user (don't auto-delete state — let the user confirm).

### Sync-driven (from `sync_tripit.py`)

The daily sync script also emits via the same `data.events` shape so SKILL.md's composition table has a single contract.

#### `tracked_flight_added`

```json
{
  "reason": "tracked_flight_added",
  "code": "XX123",
  "scheduled_dep_time": "2026-05-18T17:00:00+00:00",
  "scheduled_arr_time": "2026-05-18T20:00:00+00:00"
}
```

Fires when a flight appeared in `byair_list_trips` that wasn't on the active-flights index. The sync script writes the initial state record; the event carries the flight code + scheduled times so the agent has notification context without re-reading state.

#### `tracked_flight_removed`

```json
{
  "reason": "tracked_flight_removed",
  "code": "XX123",
  "scheduled_dep_time": "2026-05-18T17:00:00+00:00",
  "scheduled_arr_time": "2026-05-18T20:00:00+00:00"
}
```

Fires when a previously-tracked flight is no longer in `byair_list_trips` (e.g., the trip expired or the user untracked it upstream). The sync script deletes the per-flight state file — the event payload captures the prior-state metadata BEFORE the delete so the agent's composition template has `code` to render. Reading state by `flight_id` after this event arrives returns None (state already deleted).

## Composition Discipline

- One event = one notification line, unless multiple events for the same `flight_id` arrive in one cycle (then merge per the ordering rule in SKILL.md Step 3).
- Don't fabricate fields — every field above is OPTIONAL on the event dict except `reason`. Render `<missing>` or skip the substring rather than inventing a value.
- Times in the event payload are in the airport's local timezone (RFC 3339 with offset) — display as-is per byAir's instructions block ("Times are in local airport timezone (RFC3339 with offset, e.g. +02:00). Show the time as-is — do not convert or add timezone abbreviations").
