"""Delta-driven wake-event detection for the flight-assist precheck.

Pure function: given a prior flight snapshot and a fresh one (both
sourced from `byair_client.get_flight()` and trimmed to the
`last_snapshot` shape documented in `state-schema.md`), return the
list of wake events the agent should be notified about.

No I/O, no state writes, no logging. The caller (precheck.py) owns
state and decides whether `wake_agent=true` is emitted to the
scheduler. Pure-function design per `coding-policy:
script-delegation` (deterministic logic stays in scripts; reasoning
stays in the LLM — wake-rule thresholds are deterministic).

Public API:
    # The skill bundle dir is added to sys.path at invocation time; this
    # module is imported by its bare name (matches nanoclaw-core's convention).
    from wake_rules import detect_wake_events

    events = detect_wake_events(prev_snapshot, new_snapshot)
    # events = [{"reason": "gate_change", "from": "B25", "to": "B7"}, ...]

Event shapes (every event has a `reason`; other fields depend on
the rule):

    {"reason": "cancelled"}
    {"reason": "diverted"}
    {"reason": "gate_change", "side": "dep" | "arr",
     "from": "B25", "to": "B7"}
    {"reason": "delay", "delay_minutes": 22, "new_dep_time": "..."}
    {"reason": "inbound_delay_predicted", "delay_minutes": 35,
     "predicted_time": "..."}
    {"reason": "boarding_started"}
    {"reason": "carousel_revealed", "baggage": "CLM1"}

Thresholds (constants below):
    - Delay: ≥15 min change in dep_time vs prior dep_time
    - Inbound delay prediction: ≥20 min, dedupe within 5 min vs
      previously-fired magnitude
"""

from __future__ import annotations

from datetime import datetime, timezone

DELAY_THRESHOLD_MINUTES = 15
INBOUND_DELAY_THRESHOLD_MINUTES = 20
INBOUND_DELAY_DEDUPE_MINUTES = 5


def detect_wake_events(prev: dict | None, new: dict) -> list[dict]:
    """Return the list of wake events triggered by the delta `prev → new`.

    `prev` is None on the first cycle for a flight (no prior snapshot
    on disk). Rules that depend on a prior value (gate_change, delay,
    boarding_started transition, carousel_revealed transition) skip
    when prev is None. Status transitions to `cancelled` / `diverted`
    fire from a None prev too — the snapshot itself being cancelled
    is news worth a notification.
    """
    events: list[dict] = []

    new_status = new.get("computed_status")

    # Cancelled / diverted: fire on transition into the state, OR on first
    # cycle if the flight is already in that state.
    if new_status == "cancelled" and (prev is None or prev.get("computed_status") != "cancelled"):
        events.append({"reason": "cancelled"})
    if new_status == "diverted" and (prev is None or prev.get("computed_status") != "diverted"):
        events.append({"reason": "diverted"})

    # Boarding started: transition from a non-boarding state into "boarding".
    # First-cycle "already boarding" does not fire (we don't have a prior
    # to confirm the transition; the precheck's once-per-flight
    # `boarding_fired` marker handles this in phase_markers).
    if prev is not None and new_status == "boarding" and prev.get("computed_status") != "boarding":
        events.append({"reason": "boarding_started"})

    # Gate change: dep_gate or arr_gate differs from a prior non-null value.
    # First sight of a gate (None → "B25") is not a "change" — that's
    # the schedule revealing initial info, not a re-gate.
    if prev is not None:
        for side, field in (("dep", "dep_gate"), ("arr", "arr_gate")):
            old_gate = prev.get(field)
            new_gate = new.get(field)
            if old_gate is not None and new_gate is not None and old_gate != new_gate:
                events.append(
                    {"reason": "gate_change", "side": side, "from": old_gate, "to": new_gate}
                )

    # Delay: dep_time shift ≥ threshold from previously-seen dep_time.
    # Both must be present and parseable; otherwise skip.
    if prev is not None:
        delay = _delay_delta_minutes(prev.get("dep_time"), new.get("dep_time"))
        if delay is not None and abs(delay) >= DELAY_THRESHOLD_MINUTES:
            events.append(
                {"reason": "delay", "delay_minutes": delay, "new_dep_time": new["dep_time"]}
            )

    # Inbound delay prediction: only when ≥ threshold AND not previously
    # fired at a similar magnitude (within INBOUND_DELAY_DEDUPE_MINUTES).
    #
    # Dedupe only counts as "already fired" if the prior value was ALSO
    # ≥ threshold — meaning we DID fire on it. A prior value below
    # threshold did not fire, so the threshold-crossing case (e.g., 18
    # → 21 min) must still emit an event even if the magnitude shift
    # is within the dedupe window.
    new_predicted = _inbound_predicted_minutes(new)
    prev_predicted = _inbound_predicted_minutes(prev) if prev is not None else None
    if new_predicted is not None and new_predicted >= INBOUND_DELAY_THRESHOLD_MINUTES:
        already_fired_at_similar = (
            prev_predicted is not None
            and prev_predicted >= INBOUND_DELAY_THRESHOLD_MINUTES
            and abs(new_predicted - prev_predicted) < INBOUND_DELAY_DEDUPE_MINUTES
        )
        if not already_fired_at_similar:
            inbound = new.get("inbound") or {}
            events.append(
                {
                    "reason": "inbound_delay_predicted",
                    "delay_minutes": new_predicted,
                    "predicted_time": inbound.get("predicted_time"),
                }
            )

    # Carousel revealed: baggage transitions None → populated.
    if prev is not None:
        old_baggage = prev.get("baggage")
        new_baggage = new.get("baggage")
        if old_baggage is None and new_baggage is not None:
            events.append({"reason": "carousel_revealed", "baggage": new_baggage})

    return events


def _delay_delta_minutes(prev_dep_time: str | None, new_dep_time: str | None) -> int | None:
    """Compute new_dep_time - prev_dep_time in minutes, or None if either is missing.

    Both inputs are RFC 3339 strings with offsets (e.g.,
    `2026-05-17T13:00:00-07:00`). Positive return = new time is later
    (delay); negative = new time is earlier (advanced).
    """
    if not prev_dep_time or not new_dep_time:
        return None
    try:
        prev_dt = datetime.fromisoformat(prev_dep_time)
        new_dt = datetime.fromisoformat(new_dep_time)
    except ValueError:
        return None
    # Normalize to UTC for the diff so timezone-aware comparisons work
    # regardless of offset (e.g., gate change while the airport's offset
    # shifts across a DST boundary).
    if prev_dt.tzinfo is None:
        prev_dt = prev_dt.replace(tzinfo=timezone.utc)
    if new_dt.tzinfo is None:
        new_dt = new_dt.replace(tzinfo=timezone.utc)
    return int((new_dt - prev_dt).total_seconds() // 60)


def _inbound_predicted_minutes(snapshot: dict | None) -> int | None:
    """Extract the inbound delay prediction in minutes, or None when absent.

    Returns None when the snapshot has no inbound block, no predicted
    delay, or the prediction is non-numeric / negative.
    """
    if snapshot is None:
        return None
    inbound = snapshot.get("inbound")
    if not isinstance(inbound, dict):
        return None
    value = inbound.get("predicted_delay_minutes")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value <= 0:
        return None
    return value
