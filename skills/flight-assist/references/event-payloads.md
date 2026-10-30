# Precheck Wake Event Payloads

Reference for the `data.events` payload the precheck script emits on `wake_agent: true`. Each event is `{"flight_id": int | null, "event": {...}}`. `flight_id` is `null` only for the source-level events (`byair_unreachable`, `byair_recovered`, `sync_internal_error`, `sync_internal_recovered`). Consumed by `SKILL.md` Step 3 to compose user-facing notifications.

Flight poll events also carry an opaque `notification_id`. The precheck persists it before advancing the snapshot. Acknowledge each ID only after `send_message` confirms delivery, following `skills/flight-assist/references/notification-delivery.md`. Source-level sync events and other callers may omit the ID.

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

`delay_minutes` is the predicted inbound-aircraft delay; `predicted_time` is the new estimated departure. The precheck derives that clock from the flight record's scheduled departure plus the current positive inbound delay, preserving the scheduled offset. It enriches only the notification projection; the persisted schema-7 snapshot has no new field. Firing threshold and dedupe live in `wake_rules.py` (`detect_wake_events`).

#### `inbound_delay_retracted`

```json
{"reason": "inbound_delay_retracted", "prev_delay_minutes": 95, "new_delay_minutes": null}
```

`prev_delay_minutes` is the delay last surfaced to the user; `new_delay_minutes` is the current prediction (`null` when retracted entirely). Send the all-clear even if an earlier surface escalated to "rebook now" — without it, silence reads as still-delayed. Firing conditions live in `wake_rules.py` (`detect_wake_events`).

An unsent prediction that recovers produces no all-clear. If an earlier prediction was delivered, the clear refers to that last confirmed prediction. A more recent confirmed all-clear supersedes older confirmed predictions.

#### `boarding_started`

```json
{"reason": "boarding_started"}
```

Fires on transition into actual boarding, gated on the real-boarding signal rather than the `computed_status` label alone — byAir labels the phase `boarding` before boarding starts. The real-boarding predicate is also held against flight-assist's own planned boarding window, the one its boarding block uses (#295), each snapshot judged at its own poll instant. First-cycle "already boarding" does not fire. An undelivered notice retries while the live snapshot still reflects boarding. The precheck sets `phase_markers.boarding_fired` only from a confirmed receipt and suppresses subsequent transitions on that consumed flag; existing true legacy flags remain consumed. Firing conditions and the real-boarding predicate live in `wake_rules.py` (`detect_wake_events`, `is_real_boarding`); the window in `phase_markers.py` (`boarding_window_open`).

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
{"reason": "day_before", "scheduled_dep_time": "2026-09-10T19:50:00-05:00", "hours_until_dep": 7, "day_label": "today", "day_label_tz": "America/Chicago"}
```

Fires once per flight. The firing logic lives in `phase_markers.py` (`check_day_before`, `DAY_BEFORE_HOURS`); a flight first tracked late can fire the same local day.

- `hours_until_dep` — the real whole hours from the poll to scheduled departure (24 at the exact threshold, 7 in the example); never the constant 24.
- `day_label` — `today`, `tomorrow`, or a `YYYY-MM-DD` date, resolved by the precheck against the operator's local date through the core `current-tz` reader. Render it verbatim.
- `day_label_tz` — the IANA zone the label was resolved in; `null` when no operator zone was available, in which case `day_label` is the explicit date `scheduled_dep_time` carries in its own offset.

#### `time_to_leave`

```json
{"reason": "time_to_leave", "leave_by": "2026-05-18T16:15:00+00:00", "travel_time_minutes": 30, "scheduled_dep_time": "2026-05-18T17:00:00+00:00", "missed": false}
{"reason": "time_to_leave", "leave_by": "2026-05-18T16:45:00+00:00", "travel_time_minutes": 0, "scheduled_dep_time": "2026-05-18T17:00:00+00:00", "missed": true}
```

Fires when `now + travel_time + 15min buffer ≥ scheduled_dep_time`. `travel_time_minutes` is the in-traffic estimate from Google Maps Distance Matrix. `skills/flight-assist/phase_markers.py` (`check_time_to_leave`) owns timely classification, including the prior poll’s due time and the declared scheduler tick. A new snapshot’s cadence, a longer outage, or an unrecognised prior status does not grant an unbounded timely retry. Every other fire has `missed` true. SKILL.md then says the leave-by has passed and names alternatives. Suppressed when the snapshot is really boarding, taxiing / pushed back / gate-closed, already gone (`phase_markers.is_boarding_or_gone`), or a fresh GPS fix is inside the departure-airport radius. Every missed fire requires a fresh GPS fix clearly away from that airport **and** a documented pre-departure `computed_status` (`scheduled`, `check_in_open`, or `boarding` that is not real boarding). Absent, stale, or ambiguous location is not proof of being away; empty, missing, or malformed status is not proof the flight has not left (#102 / #349).

#### `gate_assignment`

```json
{"reason": "gate_assignment", "dep_gate": "E16", "dep_terminal": "2"}
```

One-time departure gate + terminal readout, fired the first cycle a gate exists inside the pre-boarding window. The window bound lives in `phase_markers.check_gate_assignment` (`GATE_ASSIGNMENT_WINDOW_LEAD_MINUTES`). `dep_terminal` is null when byAir hasn't published a terminal. Gate info before the window is recorded to state silently; gate moves after this readout surface as `gate_change` (#103).

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

### Sync-driven (from `skills/flight-assist/sync_tripit.py`)

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

Fires when a previously tracked flight disappears from `byair_list_trips` and
`skills/flight-assist/sync_tripit.py::_completed_removal` does not prove its arrival
is past. Completed expirations are quiet: no removal event and no wake. Future,
in-air and unknown-timing removals remain actionable tracking checks; an unknown
arrival must never be presented as a completed flight. Arrival equal to the
sync clock is not strictly past and also remains visible.

The payload captures prior code and scheduled times before the existing state
lifecycle runs. Every removed ID leaves the active index. A state file without
managed calendar entries is deleted; a nonempty `calendar_events` ledger remains
as the existing reconciliation tombstone. Neither quiet suppression nor a
missing state authorizes deleting that ledger or changing calendar history.

### Source-level (from `skills/flight-assist/sync_tripit.py`, #326)

These concern byAir itself, so the wrapper carries `"flight_id": null`. There is no per-flight state to read for them.

#### `byair_unreachable`

```json
{"reason": "byair_unreachable", "consecutive_failures": 3, "error": "session_missing"}
```

Fires once per failure run, when consecutive sync passes that reached byAir and failed hit the threshold (`_OUTAGE_ALERT_THRESHOLD` in `skills/flight-assist/sync_tripit.py`). `error` is the latest byAir error type (`transport_error`, `session_missing`, …). Until the sync recovers, new or changed trips do not reach the tracking index. Polling of already-tracked flights is separate; its failures show as `error: "byair_poll_failed"` in the flight-assist precheck payload.

#### `byair_recovered`

```json
{"reason": "byair_recovered", "failures_before_recovery": 5}
```

Fires on the first clean sync pass after a `byair_unreachable` alert.

#### `sync_internal_error`

```json
{"reason": "sync_internal_error", "consecutive_failures": 3, "error": "ValueError"}
```

Fires once per failure run, when consecutive passes that died on a local fault (configuration, state, or a bug) hit the same threshold. `error` is the exception class. The traceback is in the sync-tripit precheck's stderr in the host log. byAir itself is not implicated.

#### `sync_internal_recovered`

```json
{"reason": "sync_internal_recovered", "failures_before_recovery": 4}
```

Fires on the first pass that produces a result after a `sync_internal_error` alert. It means the local fault cleared, nothing more: that same pass may still fail at byAir and advance the separate byAir run.

## Composition Discipline

- One event = one notification line, unless multiple events for the same non-null `flight_id` arrive in one cycle (then merge per the ordering rule in SKILL.md Step 3).
- Source-level events (`flight_id: null`) are never merged, with each other or with flight events: each gets its own notification line.
- Don't fabricate fields — every field above is OPTIONAL on the event dict except `reason`. Render `<missing>` or skip the substring rather than inventing a value.
- Times in the event payload are in the airport's local timezone (RFC 3339 with offset) — display as-is per byAir's instructions block ("Times are in local airport timezone (RFC3339 with offset, e.g. +02:00). Show the time as-is — do not convert or add timezone abbreviations").
