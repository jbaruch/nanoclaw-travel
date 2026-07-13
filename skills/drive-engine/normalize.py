"""Source normalization — byAir record / TripIt segment → `Flight` — pure.

The union in `flight_identity` operates on a source-agnostic `Flight`. This module
builds one from each source's raw record. It is pure: the caller resolves the two
things that need I/O or fragile extraction and passes them in — the dep/arr IATA
codes (byAir via `get_airport`, TripIt via its structured segment fields) — so no
free-text airport parsing happens here (that would be the regex trap `coding-policy:
script-delegation` warns against). Times and ids come straight off the known record
shapes.

byAir wins on times (#156 Decision 4): a byAir Flight carries both the scheduled
instants and, when the last snapshot has them, the live dep/arr overlay. A TripIt
Flight carries scheduled times only.
"""

from __future__ import annotations

from datetime import datetime

from flight_identity import BYAIR, TRIPIT, Flight


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def flight_from_byair(record: dict, *, dep_iata: str, arr_iata: str) -> Flight:
    """Build a byAir `Flight` from a `flight-<id>.json` state record.

    `dep_iata` / `arr_iata` are resolved by the caller from byAir `get_airport`
    (the record carries only byAir's internal integer airport ids). Scheduled
    times come from the record; live dep/arr, when present, from `last_snapshot`
    (byAir wins). Requires `flight_id` and a parseable `scheduled_dep_time`.
    """
    flight_id = record.get("flight_id")
    if not isinstance(flight_id, int) or isinstance(flight_id, bool):
        raise ValueError(f"byAir record needs an int flight_id; got {flight_id!r}")
    sched_dep = _parse_iso(record.get("scheduled_dep_time"))
    if sched_dep is None:
        raise ValueError(f"byAir flight {flight_id} has no parseable scheduled_dep_time")
    snapshot = record.get("last_snapshot")
    snap = snapshot if isinstance(snapshot, dict) else {}
    code = record.get("code")
    trip_id = record.get("trip_id")
    return Flight(
        dep_airport=dep_iata,
        arr_airport=arr_iata,
        scheduled_dep=sched_dep,
        scheduled_arr=_parse_iso(record.get("scheduled_arr_time")),
        code=code if isinstance(code, str) else None,
        source=BYAIR,
        live_dep=_parse_iso(snap.get("dep_time")),
        live_arr=_parse_iso(snap.get("arr_time")),
        byair_flight_id=flight_id,
        trip_id=trip_id if isinstance(trip_id, int) and not isinstance(trip_id, bool) else None,
    )


def flight_from_tripit_segment(
    segment: dict,
    *,
    dep_iata: str,
    arr_iata: str,
    code: str | None = None,
    trip_id: int | None = None,
) -> Flight:
    """Build a TripIt `Flight` from a travel-schedule.json `Flight` segment.

    `dep_iata` / `arr_iata` and the optional `code` (designator) are resolved by
    the caller from the segment's structured fields; `start` / `end` are the
    scheduled instants. TripIt carries no live times. Requires a segment id
    (`uid`) and a parseable `start`.
    """
    if segment.get("type") != "Flight":
        raise ValueError(f"not a Flight segment: type={segment.get('type')!r}")
    uid = segment.get("uid")
    if not isinstance(uid, str) or not uid:
        raise ValueError("TripIt Flight segment needs a string uid")
    sched_dep = _parse_iso(segment.get("start"))
    if sched_dep is None:
        raise ValueError(f"TripIt segment {uid} has no parseable start")
    return Flight(
        dep_airport=dep_iata,
        arr_airport=arr_iata,
        scheduled_dep=sched_dep,
        scheduled_arr=_parse_iso(segment.get("end")),
        code=code,
        source=TRIPIT,
        tripit_segment_id=uid,
        trip_id=trip_id,
    )
