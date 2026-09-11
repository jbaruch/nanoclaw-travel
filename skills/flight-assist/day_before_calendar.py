"""Calendar events around a flight, for the day-before sanity check (#300).

The `day_before` compose lists the operator's calendar events near a flight.
Two parts of that are fixed predicates, not judgment, so they live here rather
than in the skill prose (`coding-policy: script-delegation`):

- Which events count. An event the operator declined (their own attendee row
  carries `responseStatus: declined`) or one its organizer cancelled is not a
  conflict. #300's notification framed a declined football practice as one.
- What clock an event reads on. Each event's instant is rendered in the
  operator's current zone, never on the event's raw offset. A source event can
  come home from a trip still carrying a `+02:00` offset while labelled
  `America/Chicago` (#300, #301): the instant is right, the offset is not the
  clock the operator is reading.

The window is `CONFLICT_MARGIN` either side of the flight's effective
departure and arrival: byAir truth when published, else scheduled — the same
instants the boarding block uses (`calendar_reconcile._effective_times`).

Pure: the caller fetches the events and resolves the operator's zone.
`scripts/day-before-calendar.py` is the caller.

stdlib-only per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from calendar_reconcile import _effective_times

# How far either side of the flight an event still counts as worth a mention.
CONFLICT_MARGIN = timedelta(hours=3)

_DISPLAY_FORMAT = "%a %b %d, %H:%M"


def _parse_instant(raw: object) -> datetime | None:
    """An RFC 3339 string as a tz-aware datetime, or None.

    A trailing `Z` is normalized first; a naive result is not an instant and
    reads as None rather than being guessed into a zone.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def conflict_window(state: dict) -> tuple[datetime, datetime]:
    """The `[start, end]` span an event must overlap to be listed.

    Raises `ValueError` when the flight's effective departure or arrival does
    not parse: a window anchored on nothing would list the wrong day.
    """
    dep_raw, arr_raw = _effective_times(state)
    dep, arr = _parse_instant(dep_raw), _parse_instant(arr_raw)
    if dep is None or arr is None:
        raise ValueError(
            f"flight {state.get('flight_id')!r} has no parseable departure/arrival "
            f"({dep_raw!r} / {arr_raw!r}) — re-run the precheck to refresh its state"
        )
    return dep - CONFLICT_MARGIN, arr + CONFLICT_MARGIN


def resolve_zone(operator_tz: str | None) -> ZoneInfo | None:
    """The operator's zone as a `ZoneInfo`, or None.

    None for no name, and for a name `ZoneInfo` cannot resolve — that one with
    a stderr line naming it. The caller reports the zone it actually used, so
    an unresolvable name is never passed on as though it were the operator's.
    """
    if not operator_tz:
        return None
    try:
        return ZoneInfo(operator_tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        print(
            f"day_before_calendar: operator zone {operator_tz!r} does not resolve ({exc}); "
            "event times keep their own offsets",
            file=sys.stderr,
        )
        return None


def _self_declined(attendees: object) -> bool:
    """True when the operator's own attendee row (`self: true`) declined."""
    if not isinstance(attendees, list):
        return False
    return any(
        isinstance(a, dict) and a.get("self") is True and a.get("responseStatus") == "declined"
        for a in attendees
    )


def _bounds(event: dict, date_zone: tzinfo) -> tuple[datetime, datetime, bool] | None:
    """`(start, end, all_day)` for an event, or None when its times do not parse.

    An all-day event's `date` is a calendar-local day, not a UTC one: it spans
    midnight to its exclusive end `date`'s midnight in `date_zone`. At UTC
    midnight instead, a Sep 10 all-day event in Chicago would end at 19:00 on
    Sep 10 and miss a flight that evening.
    """
    start, end = event.get("start"), event.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    if "date" in start and "dateTime" not in start:
        try:
            first = date.fromisoformat(str(start["date"]))
            after = date.fromisoformat(str(end.get("date", start["date"])))
        except ValueError:
            return None
        return (
            datetime(first.year, first.month, first.day, tzinfo=date_zone),
            datetime(after.year, after.month, after.day, tzinfo=date_zone),
            True,
        )
    begins, ends = _parse_instant(start.get("dateTime")), _parse_instant(end.get("dateTime"))
    if begins is None or ends is None:
        return None
    return begins, ends, False


def calendar_conflicts(
    events: list[dict],
    *,
    window: tuple[datetime, datetime],
    zone: ZoneInfo | None,
) -> tuple[list[dict], int]:
    """The events worth listing, and how many were skipped as declined/cancelled.

    Returns `(conflicts, skipped)`. A conflict is an event overlapping `window`
    that the operator has not declined and its organizer has not cancelled,
    ordered by start. Each is `{"summary", "location", "all_day", "start",
    "end", "display"}`: `start` / `end` are ISO instants on the operator's
    clock (`zone`, from `resolve_zone`), or on the event's own offset when
    `zone` is None; `display` is the human form (`Thu Sep 10, 18:00–20:10`, or
    the date for an all-day event). An all-day event's days are read in `zone`,
    else in the window's own offset. An event whose times do not parse is left
    out — there is no instant to place it by.
    """
    window_start, window_end = window
    date_zone = zone or window_start.tzinfo
    assert date_zone is not None  # conflict_window returns tz-aware bounds
    listed: list[tuple[datetime, dict]] = []
    skipped = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        bounds = _bounds(event, date_zone)
        if bounds is None:
            continue
        begins, ends, all_day = bounds
        if not (begins < window_end and window_start < ends):
            continue
        if event.get("status") == "cancelled" or _self_declined(event.get("attendees")):
            skipped += 1
            continue
        if all_day:
            display = begins.date().strftime("%a %b %d") + " (all day)"
            start_out, end_out = begins.date().isoformat(), ends.date().isoformat()
        else:
            local_begin = begins.astimezone(zone) if zone else begins
            local_end = ends.astimezone(zone) if zone else ends
            display = f"{local_begin.strftime(_DISPLAY_FORMAT)}–{local_end.strftime('%H:%M')}"
            start_out, end_out = local_begin.isoformat(), local_end.isoformat()
        location = event.get("location")
        listed.append(
            (
                begins,
                {
                    "summary": str(event.get("summary") or ""),
                    "location": location if isinstance(location, str) and location else None,
                    "all_day": all_day,
                    "start": start_out,
                    "end": end_out,
                    "display": display,
                },
            )
        )
    listed.sort(key=lambda pair: pair[0])
    return [entry for _, entry in listed], skipped
