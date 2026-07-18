"""Tests for the unified drive-block codec and three-shape reader.

Deterministic fixtures only — fixed tz-aware datetimes and hand-built events, no
wall-clock. These pin: canonical (designator-free) leg identity; a round-trip
build→parse of the new shape; the three-shape reader recognizing new + both legacy
generations for cutover; and tolerant None on malformed / unmarked events.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import (  # noqa: E402
    ALERT_GROWTH,
    ALERT_LEAVE_NOW,
    GEN_LEGACY_DP,
    GEN_LEGACY_FADRIVE,
    GEN_UNIFIED,
    build_description,
    build_extended_properties,
    canonical_flight_key,
    leg_identity,
    parse_block,
)
from chain import LegKind, PlannedLeg  # noqa: E402
from flight_identity import MergedFlight  # noqa: E402
from leg_anchor import AirportFacts, resolve_leg_anchor  # noqa: E402

UTC = timezone.utc


def _dt(h, mi=0, *, day=12):
    return datetime(2020, 7, day, h, mi, tzinfo=UTC)


def ext_event(ext: dict) -> dict:
    """Wrap a `build_extended_properties` result as the event-level key.

    `build_extended_properties` returns `{"private": {...}}`; a fetched event
    nests that under `extendedProperties`.
    """
    return {"extendedProperties": ext}


def flight(dep, arr, sched_dep, sched_arr=None, *, fid=1, ids=None):
    return MergedFlight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=sched_dep,
        scheduled_arr=sched_arr or (sched_dep + timedelta(hours=2)),
        live_dep=None,
        live_arr=None,
        code=None,
        byair_flight_ids=frozenset(ids or {fid}),
    )


# --- canonical identity -----------------------------------------------------


def test_canonical_key_is_route_plus_instant_not_designator():
    f1 = flight("STN", "CPH", _dt(9), fid=1)
    f2 = flight("STN", "CPH", _dt(9), fid=2)  # same route+instant, different id
    assert canonical_flight_key(f1) == "STN-CPH-20200712T0900Z"
    assert canonical_flight_key(f1) == canonical_flight_key(f2)


def test_transfer_identity_is_the_flight_pair():
    n = flight("LHR", "LHR", _dt(9), _dt(10), fid=1)
    n1 = flight("LGW", "JFK", _dt(16), _dt(19), fid=2)
    leg = resolve_leg_anchor(
        PlannedLeg(LegKind.AIRPORT_TRANSFER, from_flight=n, to_flight=n1),
        facts=AirportFacts(dep_flag="🇺🇸", arr_flag="🇬🇧"),
        partner_facts=AirportFacts(dep_flag="🇬🇧", arr_flag="🇺🇸"),
    )
    assert leg_identity(leg) == "LHR-LHR-20200712T0900Z|LGW-JFK-20200712T1600Z"


def test_departure_identity_is_the_single_flight():
    f = flight("BNA", "JFK", _dt(9))
    leg = resolve_leg_anchor(
        PlannedLeg(LegKind.AIRPORT_DEPARTURE, to_flight=f),
        facts=AirportFacts(dep_flag="🇺🇸", arr_flag="🇺🇸"),
    )
    assert leg_identity(leg) == "BNA-JFK-20200712T0900Z"


# --- build → parse round trip (unified) -------------------------------------


def test_build_parse_round_trip():
    desc = build_description(
        summary="Drive: → BNA (DL4908)",
        identity="BNA-JFK-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=1500,
        anchor=_dt(8, 0),
        origin="Hotel X",
        destination="BNA",
        alerted={ALERT_GROWTH},
    )
    event = {"id": "evt1", "description": desc}
    parsed = parse_block(event)
    assert parsed is not None
    assert parsed.generation == GEN_UNIFIED
    assert parsed.event_id == "evt1"
    assert parsed.identity == "BNA-JFK-20200712T0900Z"
    assert parsed.kind == "airport_departure"
    assert parsed.baseline_seconds == 1500
    assert parsed.anchor == _dt(8, 0)
    assert parsed.origin == "Hotel X"
    assert parsed.destination == "BNA"
    assert parsed.alerted == frozenset({ALERT_GROWTH})


def test_transfer_round_trip_carries_window_end():
    desc = build_description(
        summary="Drive: LHR → LGW",
        identity="LHR-LHR-20200712T0900Z|LGW-JFK-20200712T1600Z",
        kind="airport_transfer",
        baseline_seconds=3600,
        anchor=_dt(11, 0),
        origin="LHR",
        destination="LGW",
        window_end=_dt(14, 0),
        alerted={ALERT_GROWTH, ALERT_LEAVE_NOW},
    )
    parsed = parse_block({"id": "evtT", "description": desc})
    assert parsed is not None
    assert parsed.window_end == _dt(14, 0)
    assert parsed.alerted == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


# --- dual-source reader: extendedProperties (the #178 migration) ------------


def test_extended_properties_build_parse_round_trip():
    ext = build_extended_properties(
        identity="BNA-JFK-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=1500,
        anchor=_dt(8, 0),
        origin="Hotel X",
        destination="BNA",
        alerted={ALERT_GROWTH},
    )
    # Only the human line stays in the description; the machine state is in the map.
    event = {"id": "evt1", "description": "Drive: → BNA (DL4908)", **ext_event(ext)}
    parsed = parse_block(event)
    assert parsed is not None
    assert parsed.generation == GEN_UNIFIED
    assert parsed.event_id == "evt1"
    assert parsed.identity == "BNA-JFK-20200712T0900Z"
    assert parsed.kind == "airport_departure"
    assert parsed.baseline_seconds == 1500  # parsed back from its string form
    assert parsed.anchor == _dt(8, 0)
    assert parsed.origin == "Hotel X"
    assert parsed.destination == "BNA"
    assert parsed.alerted == frozenset({ALERT_GROWTH})


def test_extended_properties_values_are_all_strings():
    # extendedProperties.private accepts only string values — the builder must
    # stringify the int baseline and the datetimes, or Calendar rejects the write.
    ext = build_extended_properties(
        identity="BNA-JFK-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=1500,
        anchor=_dt(8, 0),
        origin="Hotel X",
        destination="BNA",
        window_end=_dt(9, 0),
    )
    assert all(isinstance(v, str) for v in ext["private"].values())


def test_extended_transfer_round_trip_carries_window_end():
    ext = build_extended_properties(
        identity="LHR-LHR-20200712T0900Z|LGW-JFK-20200712T1600Z",
        kind="airport_transfer",
        baseline_seconds=3600,
        anchor=_dt(11, 0),
        origin="LHR",
        destination="LGW",
        window_end=_dt(14, 0),
        alerted={ALERT_GROWTH, ALERT_LEAVE_NOW},
    )
    parsed = parse_block({"id": "evtT", **ext_event(ext)})
    assert parsed is not None
    assert parsed.window_end == _dt(14, 0)
    assert parsed.alerted == frozenset({ALERT_GROWTH, ALERT_LEAVE_NOW})


def test_extended_properties_read_with_no_description_at_all():
    # A block that carries state ONLY in extendedProperties (post-flip shape,
    # human line absent) still round-trips.
    ext = build_extended_properties(
        identity="BNA-JFK-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=1500,
        anchor=_dt(8, 0),
        origin="Hotel X",
        destination="BNA",
    )
    parsed = parse_block({"id": "evt1", **ext_event(ext)})
    assert parsed is not None
    assert parsed.generation == GEN_UNIFIED
    assert parsed.identity == "BNA-JFK-20200712T0900Z"


def test_extended_properties_preferred_over_description():
    # A block carrying BOTH (transition window) reads its state from
    # extendedProperties, not the description.
    ext = build_extended_properties(
        identity="EXT-IDENTITY-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=1500,
        anchor=_dt(8, 0),
        origin="ExtOrigin",
        destination="BNA",
    )
    desc = build_description(
        summary="Drive: → BNA",
        identity="DESC-IDENTITY-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=999,
        anchor=_dt(7, 0),
        origin="DescOrigin",
        destination="BNA",
    )
    parsed = parse_block({"id": "evt1", "description": desc, **ext_event(ext)})
    assert parsed is not None
    assert parsed.identity == "EXT-IDENTITY-20200712T0900Z"
    assert parsed.origin == "ExtOrigin"
    assert parsed.baseline_seconds == 1500


def test_extended_unknown_version_falls_back_to_description():
    # An extendedProperties map at a version this codec does not accept is "no
    # unified state here" — the reader falls back to a valid description.
    ext = build_extended_properties(
        identity="EXT-IDENTITY-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=1500,
        anchor=_dt(8, 0),
        origin="ExtOrigin",
        destination="BNA",
    )
    ext["private"]["dengine_schema_version"] = "999"
    desc = build_description(
        summary="Drive: → BNA",
        identity="DESC-IDENTITY-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=999,
        anchor=_dt(7, 0),
        origin="DescOrigin",
        destination="BNA",
    )
    parsed = parse_block({"id": "evt1", "description": desc, **ext_event(ext)})
    assert parsed is not None
    assert parsed.identity == "DESC-IDENTITY-20200712T0900Z"


def test_extended_missing_leg_identity_falls_back_to_description():
    ext = build_extended_properties(
        identity="EXT-IDENTITY-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=1500,
        anchor=_dt(8, 0),
        origin="ExtOrigin",
        destination="BNA",
    )
    del ext["private"]["dengine_leg"]  # version matches but no identity → fall back
    desc = build_description(
        summary="Drive: → BNA",
        identity="DESC-IDENTITY-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=999,
        anchor=_dt(7, 0),
        origin="DescOrigin",
        destination="BNA",
    )
    parsed = parse_block({"id": "evt1", "description": desc, **ext_event(ext)})
    assert parsed is not None
    assert parsed.identity == "DESC-IDENTITY-20200712T0900Z"


def test_unrelated_extended_properties_ignored_description_still_read():
    # A neighbour tool's private props (no dengine_* keys) must not shadow the
    # description reader.
    desc = build_description(
        summary="Drive: → BNA",
        identity="DESC-IDENTITY-20200712T0900Z",
        kind="airport_departure",
        baseline_seconds=1200,
        anchor=_dt(8, 0),
        origin="home",
        destination="BNA",
    )
    event = {
        "id": "evt1",
        "description": desc,
        "extendedProperties": {"private": {"someOtherTool": "x"}},
    }
    parsed = parse_block(event)
    assert parsed is not None
    assert parsed.identity == "DESC-IDENTITY-20200712T0900Z"


# --- three-shape reader (cutover) -------------------------------------------


def test_reads_legacy_fadrive_block():
    desc = (
        "Drive: → CPH (SK915)\n"
        "[flight-assist:flight=3358446:dir=to_airport]\n"
        '<!--fadrive:{"schema_version":1,"b":900,"a":"2020-07-12T08:00:00+00:00",'
        '"o":"here","d":"CPH","al":""}-->'
    )
    parsed = parse_block({"id": "old1", "description": desc})
    assert parsed is not None
    assert parsed.generation == GEN_LEGACY_FADRIVE
    assert parsed.legacy_id == "3358446"
    assert parsed.legacy_direction == "to_airport"


def test_reads_legacy_dp_block():
    desc = (
        "Drive: Keynote\n"
        "[drive-planner:meeting=abc123:dir=outbound]\n"
        '<!--dp:{"v":2,"b":600,"a":"2020-07-12T08:00:00+00:00","o":"home","d":"venue","al":""}-->'
    )
    parsed = parse_block({"id": "old2", "description": desc})
    assert parsed is not None
    assert parsed.generation == GEN_LEGACY_DP
    assert parsed.legacy_id == "abc123"
    assert parsed.legacy_direction == "outbound"


# --- tolerance --------------------------------------------------------------


def test_unmarked_event_is_none():
    assert parse_block({"id": "x", "description": "Just a normal meeting"}) is None


def test_missing_description_is_none():
    assert parse_block({"id": "x"}) is None
    assert parse_block("not a dict") is None
    assert parse_block(None) is None


def test_unified_marker_with_corrupt_state_still_identifies():
    desc = (
        "Drive: → BNA\n"
        "[drive-engine:leg=BNA-JFK-20200712T0900Z:kind=airport_departure]\n"
        "<!--dengine:{bad json-->"
    )
    parsed = parse_block({"id": "e", "description": desc})
    assert parsed is not None
    assert parsed.generation == GEN_UNIFIED
    assert parsed.identity == "BNA-JFK-20200712T0900Z"
    assert parsed.anchor is None  # unparseable state → no usable fields


def test_unknown_version_treated_as_no_usable_state():
    desc = (
        "Drive: → BNA\n[drive-engine:leg=BNA-JFK-20200712T0900Z:kind=airport_departure]\n"
        '<!--dengine:{"schema_version":999,"a":"2020-07-12T08:00:00+00:00"}-->'
    )
    parsed = parse_block({"id": "e", "description": desc})
    assert parsed is not None
    assert parsed.generation == GEN_UNIFIED
    assert parsed.anchor is None  # version mismatch → fields dropped
