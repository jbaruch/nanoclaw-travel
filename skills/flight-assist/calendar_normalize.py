"""Normalize raw Google Calendar events into the planner's event shape.

The reconcile script (PR3b) fetches events via Composio's
`GOOGLECALENDAR_FIND_EVENT`, which returns Google Calendar event resources.
`plan_reconciliation` (see `calendar_plan.py`) consumes a flat normalized
shape: `{event_id, calendar_id, summary, start, end, private_props,
is_reclaim_travel}`. This module is the deterministic adapter between the
two — pure, stdlib-only, unit-tested against the real event shapes.

is_reclaim_travel is content-based, not calendar-based. Reclaim writes its
travel blocks onto the user's primary calendar interleaved with real
meetings (there is no dedicated Reclaim calendar — see #55), so the only
safe delete discriminator is the event's own content: the Reclaim
authorship signature in the description plus a travel marker in the
summary. The planner further bounds every delete to a same-airport layover
gap, so a genuine meeting is never a delete candidate even if it somehow
carried the signature.

A real Reclaim travel block looks like:

    summary:     "🚌 Travel"
    description: "...created by <a href='https://app.reclaim.ai/...'>Reclaim</a>...
                  Baruch is traveling to/from the airport for a flight..."

A real Flighty flight event looks like:

    summary:     "✈ BNA→YYZ • UA 8018"
    start/end:   {"dateTime": "2026-06-26T10:05:00-05:00", "timeZone": "..."}
    (no extendedProperties — untagged, an adoption candidate)

stdlib-only per `coding-policy: dependency-management`.
"""

from __future__ import annotations

# Distinctive substring of the authorship link Reclaim stamps into every
# event description. Two-factor with the travel marker below so a user
# event is never misread as a Reclaim travel block.
RECLAIM_SIGNATURE = "app.reclaim.ai"

# Reclaim names every travel block "🚌 Travel"; its habit / focus / task
# blocks carry other summaries. Matching the word travel in the summary (on
# a Reclaim-signed event) separates travel from those non-travel blocks.
_TRAVEL_MARKER = "travel"


class NormalizeError(ValueError):
    """Raised when a raw event lacks a field normalization needs (e.g. id).

    A ValueError subclass: the fix is "pass a well-formed Google event",
    not "retry".
    """


def is_reclaim_travel(raw_event: dict) -> bool:
    """True when the event is a Reclaim-generated travel block.

    Two factors, both required:
      1. the description carries Reclaim's authorship signature, AND
      2. the summary names it a travel block.

    Reclaim's non-travel blocks (habit / focus / task) carry the signature
    but a different summary, so they return False. A user's own event
    carries neither and returns False.
    """
    description = (raw_event.get("description") or "").lower()
    summary = (raw_event.get("summary") or "").lower()
    return RECLAIM_SIGNATURE in description and _TRAVEL_MARKER in summary


def _extract_instant(endpoint: dict | None) -> str | None:
    """Pull the RFC 3339 instant from a Google event start/end block.

    Google uses `{"dateTime": "...+offset", "timeZone": "..."}` for timed
    events and `{"date": "YYYY-MM-DD"}` for all-day events. Returns the
    `dateTime` when present (it carries the offset the planner needs), else
    the bare `date` (which the planner will reject — all-day events are not
    flights or travel blocks and the reconcile filters them out before
    planning), else None.
    """
    if not isinstance(endpoint, dict):
        return None
    return endpoint.get("dateTime") or endpoint.get("date")


def normalize_event(raw_event: dict, *, calendar_id: str, classify_reclaim: bool = False) -> dict:
    """Flatten one Google Calendar event into the planner's event shape.

    Args:
        raw_event: a Google Calendar event resource.
        calendar_id: the calendar the event was fetched from (the planner
            classifies by calendar ID, so this is authoritative — not read
            from the event body).
        classify_reclaim: when True, run the Reclaim-travel content
            classifier and set `is_reclaim_travel`. Pass True only for
            events fetched from the calendar Reclaim writes to (the user's
            primary); False for the byAir flight calendar.

    Returns the flat shape `plan_reconciliation` consumes. `private_props`
    is `extendedProperties.private` (the tags flight-assist stamps), `{}`
    when absent.

    Raises NormalizeError when the event has no `id`.
    """
    event_id = raw_event.get("id")
    if not event_id:
        raise NormalizeError(f"event has no id: {raw_event!r}")
    extended = raw_event.get("extendedProperties") or {}
    private_props = extended.get("private") if isinstance(extended, dict) else None
    return {
        "event_id": event_id,
        "calendar_id": calendar_id,
        "summary": raw_event.get("summary") or "",
        "start": _extract_instant(raw_event.get("start")),
        "end": _extract_instant(raw_event.get("end")),
        "private_props": private_props or {},
        "is_reclaim_travel": is_reclaim_travel(raw_event) if classify_reclaim else False,
    }
