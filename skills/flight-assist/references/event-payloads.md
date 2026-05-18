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
```

`delay_minutes` is `new_dep_time − prev_dep_time` in minutes. Positive = delayed, negative = moved earlier. Threshold ≥15 min (absolute).

#### `inbound_delay_predicted`

```json
{"reason": "inbound_delay_predicted", "delay_minutes": 35, "predicted_time": "2026-05-17T13:00:00-07:00"}
```

Fires when byAir's `inbound.predicted_delay.delay_minutes` ≥ 20 min, with dedupe-within-5-min vs prior firing magnitude.

#### `boarding_started`

```json
{"reason": "boarding_started"}
```

Fires on `computed_status` transition into `boarding`. First-cycle "already boarding" does not fire — the `phase_markers.boarding_fired` gate handles that case via `boarding` phase marker (not implemented in v0.1 — boarding-fired is reserved for a future T-30min boarding-prep notification).

#### `carousel_revealed`

```json
{"reason": "carousel_revealed", "baggage": "CLM1"}
```

Fires when `baggage` transitions `null` → populated.

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

Fires when `now + travel_time + 15min buffer ≥ scheduled_dep_time`. `travel_time_minutes` is the in-traffic estimate from Google Maps Distance Matrix.

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

## Composition Discipline

- One event = one notification line, unless multiple events for the same `flight_id` arrive in one cycle (then merge per the ordering rule in SKILL.md Step 3).
- Don't fabricate fields — every field above is OPTIONAL on the event dict except `reason`. Render `<missing>` or skip the substring rather than inventing a value.
- Times in the event payload are in the airport's local timezone (RFC 3339 with offset) — display as-is per `coding-policy: temporal-awareness` ("Times are in local airport timezone"), don't convert to user's home tz.
