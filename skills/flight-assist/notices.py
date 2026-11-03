"""Render flight notices from precheck events (#370).

Every reason the precheck and the TripIt sync emit has a fixed English
template here. The wording matches what the model composed before. Times
show as their own airport clock. `compose` turns one cycle's events into the
messages to send: one per flight, ordered cancel/divert first, then
time-sensitive, then information, then logistics. Each source-level event
(`flight_id: null`) is its own message. A message lists the notification ids
it covers.

A missing optional field drops its clause, never invents a value. An
unknown reason raises ValueError: every reason is ours, so it is a defect.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

ORDER = {
    "cancelled": 0,
    "diverted": 0,
    "removed_upstream": 0,
    "boarding_started": 1,
    "time_to_leave": 1,
    "connection_at_risk": 1,
    "gate_assignment": 2,
    "gate_change": 2,
    "delay": 2,
    "inbound_delay_predicted": 2,
    "inbound_delay_retracted": 2,
    "day_before": 2,
    "tracked_flight_removed": 2,
    "carousel_revealed": 3,
    "arrival_logistics": 3,
}
SOURCE_REASONS = frozenset(
    {"byair_unreachable", "byair_recovered", "sync_internal_error", "sync_internal_recovered"}
)
SILENT = frozenset({"tracked_flight_added"})

Event = dict[str, Any]
Calendar = Callable[[int], dict | None]


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def clock(value: Any) -> str | None:
    moment = _when(value)
    return moment.strftime("%H:%M") if moment else None


def _day(label: Any, departure: datetime | None) -> str:
    if label in ("today", "tomorrow"):
        return str(label).capitalize()
    moment = _when(f"{label}T00:00:00+00:00") if isinstance(label, str) else departure
    moment = moment or departure
    return f"{moment:%a} {moment.month}/{moment.day}" if moment else "Soon"


def _day_before(code: str, event: Event, state: dict, calendar: dict | None) -> str:
    snap = state.get("last_snapshot") or {}
    departure = _when(event.get("scheduled_dep_time") or state.get("scheduled_dep_time"))
    route = " → ".join(x for x in (snap.get("dep_airport_code"), snap.get("arr_airport_code")) if x)
    text = f"{_day(event.get('day_label'), departure)}: {code}"
    text += f" {route}" if route else ""
    text += f", departs {departure:%H:%M}." if departure else "."
    items = (calendar or {}).get("events") or []
    lines = []
    for item in items:
        summary = item.get("summary") or "(untitled)"
        if item.get("window"):
            closes = clock(item.get("end"))
            lines.append(
                f"• {summary}: drop-in window you can't reach before it closes"
                + (f" at {closes}" if closes else "")
            )
        else:
            lines.append(f"• {summary} {item.get('display') or ''}".rstrip())
    if lines:
        text += "\nNear it on your calendar:\n" + "\n".join(lines)
    return text


def _arrival(code: str, state: dict) -> str:
    snap = state.get("last_snapshot") or {}
    where = snap.get("arr_airport_name") or snap.get("arr_airport_code")
    when = clock(snap.get("arr_time") or state.get("scheduled_arr_time"))
    text = (
        f"{code} lands"
        + (f" at {where}" if where else "")
        + (f" around {when}" if when else "")
        + "."
    )
    place = ", ".join(
        f"{label} {value}"
        for label, value in (("Terminal", snap.get("arr_terminal")), ("Gate", snap.get("arr_gate")))
        if value
    )
    if place:
        text += f" {place}."
    if snap.get("baggage"):
        text += f" Bags: carousel {snap['baggage']}."
    return text


def line(
    event: Event, state: dict | None, calendar: dict | None = None, *, announce_added: bool = False
) -> str | None:
    """One event's sentence, or None for a silent reason. A new tracked flight
    is announced only when the batch carries nothing else."""
    reason = event.get("reason")
    state = state or {}
    snap = state.get("last_snapshot") or {}
    code = state.get("code") or event.get("code") or "your flight"
    if reason in SILENT:
        if not announce_added:
            return None
        date = (event.get("scheduled_dep_time") or "")[:10]
        return f"Now tracking {code}" + (f" on {date}." if date else ".")
    if reason == "cancelled":
        return f"Flight {code} cancelled."
    if reason == "diverted":
        return f"Flight {code} diverted."
    if reason == "removed_upstream":
        return (
            f"Flight {code} no longer tracked upstream — remove from active trips if intentional."
        )
    if reason == "gate_assignment":
        gate, terminal = event.get("dep_gate"), event.get("dep_terminal")
        where = f"Terminal {terminal}, Gate {gate}" if terminal else f"Gate {gate}"
        return f"{code} departs {where}."
    if reason == "gate_change":
        side = "Arrival gate" if event.get("side") == "arr" else "Gate"
        if event.get("to") is None:
            return f"{side} {event.get('from')} removed from {code}."
        return f"{side} change: {code} moved from {event.get('from')} to {event.get('to')}."
    if reason == "delay":
        minutes = int(event.get("delay_minutes") or 0)
        when = clock(event.get("new_dep_time"))
        moved = f"delayed by {minutes} min" if minutes >= 0 else f"moved earlier by {-minutes} min"
        return f"Flight {code} {moved}." + (f" New departure: {when} (local)." if when else "")
    if reason == "inbound_delay_predicted":
        when = clock(event.get("predicted_time"))
        return f"Inbound aircraft delay predicted: {event.get('delay_minutes')} min for {code}." + (
            f" New estimated departure: {when}." if when else ""
        )
    if reason == "inbound_delay_retracted":
        now = event.get("new_delay_minutes")
        status = "no longer predicted" if now is None else f"now {now} min"
        return (
            f"Inbound delay for {code} has cleared — previously predicted "
            f"{event.get('prev_delay_minutes')} min, {status}. {code} is back on track."
        )
    if reason == "connection_at_risk":
        leg1, leg2 = event.get("leg1_code") or "the inbound flight", event.get("leg2_code") or code
        if event.get("missed_connection"):
            return (
                f"Connection structurally missed: {leg1} projected to arrive at or after "
                f"{leg2} departs. Rebook required."
            )
        return (
            f"Tight connection: {leg2} boards in {event.get('transfer_minutes_remaining')} min "
            f"after {leg1} arrives — below the {event.get('min_transfer_minutes')}-min buffer. "
            "Consider rebooking now."
        )
    if reason == "boarding_started":
        place = ", ".join(
            f"{label} {value}"
            for label, value in (
                ("Gate", snap.get("dep_gate")),
                ("terminal", snap.get("dep_terminal")),
            )
            if value
        )
        return f"Boarding now: {code}." + (f" {place}." if place else "")
    if reason == "carousel_revealed":
        return f"Baggage carousel for {code}: {event.get('baggage')}."
    if reason == "time_to_leave":
        departure = _when(event.get("scheduled_dep_time") or state.get("scheduled_dep_time"))
        leave = _when(event.get("leave_by"))
        if leave and departure:
            leave = leave.astimezone(departure.tzinfo)  # the departure airport's clock
        at = f"{leave:%H:%M}" if leave else "now"
        drive = event.get("travel_time_minutes")
        if event.get("missed"):
            return (
                f"Leave-by {at} for {code} has passed ({drive} min drive). "
                "Look at alternatives — this flight is still listed as not gone."
            )
        return f"Leave by {at} to make {code} ({drive} min drive with current traffic)."
    if reason == "day_before":
        return _day_before(code, event, state, calendar)
    if reason == "arrival_logistics":
        return _arrival(code, state)
    if reason == "tracked_flight_removed":
        date = (event.get("scheduled_dep_time") or "")[:10]
        on = f" on {date}" if date else ""
        return (
            f"Flight {code}{on} disappeared from tracking before completion could be confirmed; "
            "check whether it was cancelled or re-tracked."
        )
    if reason == "byair_unreachable":
        return (
            f"Flight sync with byAir is failing: {event.get('consecutive_failures')} passes in a "
            f"row, last error {event.get('error')}. New or changed trips won't reach flight "
            "tracking until it recovers. Details are in the sync-tripit host log."
        )
    if reason == "byair_recovered":
        return "Flight sync with byAir recovered."
    if reason == "sync_internal_error":
        return (
            f"Flight sync is failing inside NanoClaw: {event.get('consecutive_failures')} passes "
            f"in a row, last error {event.get('error')}. The traceback is in the sync-tripit "
            "host log."
        )
    if reason == "sync_internal_recovered":
        return "The local fault in flight sync has cleared."
    raise ValueError(f"no notice template for reason {reason!r}")


def compose(
    items: list[dict],
    read_state: Callable[[int], dict | None],
    read_calendar: Calendar,
) -> list[tuple[list[str], str]]:
    """The messages for one cycle: [(notification ids, text), ...]."""
    flights: dict[int, list[dict]] = {}
    messages: list[tuple[list[str], str]] = []
    only_adds = bool(items) and all(item["event"].get("reason") in SILENT for item in items)
    for item in items:
        if item.get("flight_id") is None:
            text = line(item["event"], None)
            if text:
                messages.append(
                    ([item["notification_id"]] if item.get("notification_id") else [], text)
                )
        else:
            flights.setdefault(item["flight_id"], []).append(item)
    for flight_id, group in flights.items():
        state = read_state(flight_id)
        group.sort(key=lambda item: ORDER.get(item["event"].get("reason"), 2))
        calendar = (
            read_calendar(flight_id)
            if any(item["event"].get("reason") == "day_before" for item in group)
            else None
        )
        lines = [line(item["event"], state, calendar, announce_added=only_adds) for item in group]
        text = "\n".join(text for text in lines if text)
        ids = [item["notification_id"] for item in group if item.get("notification_id")]
        if text or ids:
            messages.append((ids, text))
    return messages
