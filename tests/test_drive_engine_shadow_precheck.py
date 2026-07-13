"""Tests for the shadow-precheck core (build_shadow_result).

Deterministic fixtures only — hand-built byAir records, a fake airport resolver
and router, fixed reference `now`, no wall-clock. These pin the read-only shadow
behavior: the plan is assembled from records and diffed against the current
blocks, unresolvable airports are skipped (not guessed), and the payload never
wakes the agent. The main() I/O layer is the outer process boundary and is not
unit-tested here.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from block_codec import GEN_LEGACY_FADRIVE, ParsedBlock  # noqa: E402
from shadow_precheck import ResolvedAirport, build_shadow_result  # noqa: E402

UTC = timezone.utc
HOME = "12 Example St, TN"
NOW = datetime(2020, 7, 10, 12, 0, tzinfo=UTC)
US = "🇺🇸"

_IATA = {1: "STN", 2: "CPH", 3: "JFK", 4: "BNA"}


def _resolve_airport(airport_id):
    iata = _IATA.get(airport_id)
    if iata is None:
        return None
    return ResolvedAirport(iata=iata, flag=US, delay_index="low")


def _route(_o, _d):
    return timedelta(minutes=30)


def _record(fid, code, dep_id, arr_id, dep, arr, *, trip_id=7):
    return {
        "schema_version": 6,
        "flight_id": fid,
        "code": code,
        "trip_id": trip_id,
        "scheduled_dep_time": dep,
        "scheduled_arr_time": arr,
        "dep_airport_id": dep_id,
        "arr_airport_id": arr_id,
        "last_snapshot": None,
    }


def legacy(flight_id, direction, event_id):
    return ParsedBlock(
        generation=GEN_LEGACY_FADRIVE,
        event_id=event_id,
        legacy_id=flight_id,
        legacy_direction=direction,
    )


def test_shadow_payload_never_wakes():
    records = [_record(1, "AA1", 4, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_shadow_result(
        flight_records=records,
        resolve_airport=_resolve_airport,
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    assert result.payload["wake_agent"] is False
    assert "counts" in result.payload["data"]


def test_shadow_jul12_itinerary_diff():
    records = [
        _record(6277117, "FR7382", 1, 2, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z"),
        _record(3358446, "SK915", 2, 3, "2020-07-12T13:00:00Z", "2020-07-12T20:00:00Z"),
        _record(3359520, "DL4908", 3, 4, "2020-07-12T22:00:00Z", "2020-07-12T23:30:00Z"),
    ]
    current = (
        [legacy("6277117", "to_airport", f"stn{i}") for i in range(5)]
        + [legacy("3358446", "to_airport", f"cph{i}") for i in range(7)]
        + [legacy("3359520", "to_airport", "jfk1")]
    )
    result = build_shadow_result(
        flight_records=records,
        resolve_airport=_resolve_airport,
        current_blocks=current,
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    # STN departure converges its 5 legacy blocks; CPH (7) + JFK (1) delete as orphans
    assert result.counts["converts"] == 1
    assert result.counts["legacy_converted"] == 5
    assert result.counts["deletes"] == 8
    assert result.payload["wake_agent"] is False
    assert "CONVERT" in result.rendered and "DELETE" in result.rendered


def test_unresolved_airport_is_skipped_not_guessed():
    # airport_id 9 is unknown to the resolver.
    records = [_record(1, "AA1", 9, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_shadow_result(
        flight_records=records,
        resolve_airport=_resolve_airport,
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    assert result.plan.is_noop  # no flight built → nothing desired
    assert any("unresolved airport" in s for s in result.skipped)


def test_route_failure_skips_with_diagnostic():
    records = [_record(1, "AA1", 4, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_shadow_result(
        flight_records=records,
        resolve_airport=_resolve_airport,
        current_blocks=[],
        route=lambda _o, _d: None,
        now=NOW,
        home_address=HOME,
    )
    assert result.plan.creates == ()
    assert len(result.skipped) >= 1
