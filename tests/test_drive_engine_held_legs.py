"""Held legs (#334): a leg the engine could not compute keeps its block.

On 2026-10-19 Google Distance Matrix failed for a few minutes, the router fell
back to TomTom, and TomTom geocoded "SFO airport" as the town of Airport, MO.
The implausible legs were skipped, a skipped leg left the desired set, and the
reconcile orphan-deleted thirteen correct blocks. These tests pin the fix:

  - a leg that cannot be computed (route failed, implausible, unresolved
    position, past) is held, and its current block is neither deleted nor
    updated; a deliberately suppressed leg (a trivial drive) still deletes
  - meeting and lodging legs hold the same way
  - airports route by their byAir coordinates; a backup-provider answer for a
    text airport is refused, so the leg holds
  - a held leg with no block is announced once it has been missing an hour
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

# E402: the skill modules resolve only after the sys.path inserts above.
import reconcile_sweep  # noqa: E402
import unplanned_notices  # noqa: E402
from airport_drive_inputs import airport_context  # noqa: E402
from block_codec import GEN_UNIFIED, ParsedBlock  # noqa: E402
from engine import AirportInfo, build_reconcile_plan  # noqa: E402
from flight_identity import BYAIR, TRIPIT, Flight  # noqa: E402
from lodging_source import KIND_OUTBOUND, KIND_RETURN, DriveTrip, lodging_desired_blocks  # noqa: E402
from maps_client import TomTomClient, TravelTime  # noqa: E402
from meeting_source import meeting_desired_blocks  # noqa: E402
from reconcile import DesiredBlock, HeldLeg, plan_reconcile  # noqa: E402
from scan import MeetingClass, TransitLeg  # noqa: E402

HOME = "12 Example St, TN"
NOW = datetime(2025, 10, 19, 20, 30, tzinfo=UTC)
US = "🇺🇸"


def _flight(dep="BNA", arr="SFO", *, fid=1, day=22):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=datetime(2025, 10, day, 15, 0, tzinfo=UTC),
        scheduled_arr=datetime(2025, 10, day, 19, 0, tzinfo=UTC),
        source=BYAIR,
        byair_flight_id=fid,
    )


def _info(**coords):
    return {
        code: AirportInfo(flag=US, timezone="America/Chicago", lat=lat, lon=lon)
        for code, (lat, lon) in coords.items()
    }


def _block(identity, kind, event_id="evt-1"):
    return ParsedBlock(
        generation=GEN_UNIFIED,
        event_id=event_id,
        identity=identity,
        kind=kind,
        anchor=NOW + timedelta(days=1),
        origin=HOME,
        destination="BNA airport",
        baseline_seconds=2400,
    )


def _plan(route, *, current, now=NOW, info=None, flights=None):
    return build_reconcile_plan(
        flights=flights or [_flight()],
        airport_info=info or _info(BNA=(36.12, -86.68), SFO=(37.62, -122.38)),
        current_blocks=current,
        route=route,
        home_address=HOME,
        now=now,
    )


def _identity_of(kind, route=lambda o, d: timedelta(minutes=40)):
    """The engine's identity for this flight's leg of `kind`."""
    result = _plan(route, current=[])
    return next(c.desired.identity for c in result.plan.creates if c.desired.kind == kind)


# --- the reconcile diff -------------------------------------------------------


def test_a_held_key_keeps_its_block_and_an_unheld_orphan_goes():
    current = [_block("a", "airport_departure", "keep"), _block("b", "airport_departure", "go")]
    plan = plan_reconcile([], current, held=frozenset({("a", "airport_departure")}))
    assert [d.event_id for d in plan.deletes] == ["go"]
    assert plan.updates == ()


# --- airport legs -------------------------------------------------------------


def test_a_failed_route_keeps_the_existing_airport_blocks():
    dep = _identity_of("airport_departure")
    arr = _identity_of("airport_arrival")
    current = [_block(dep, "airport_departure", "d"), _block(arr, "airport_arrival", "a")]
    result = _plan(lambda o, d: None, current=current)
    assert result.plan.deletes == () and result.plan.updates == ()
    assert {leg.kind for leg in result.held} == {"airport_departure", "airport_arrival"}


def test_an_implausible_drive_keeps_the_existing_block():
    """The Airport, MO answer: a 28.8 h "drive" is held, not a reason to delete."""
    dep = _identity_of("airport_departure")
    result = _plan(lambda o, d: timedelta(hours=28.8), current=[_block(dep, "airport_departure")])
    assert result.plan.deletes == ()
    assert any("implausible" in leg.reason for leg in result.held)


def test_a_past_leg_keeps_its_block():
    dep = _identity_of("airport_departure")
    after = datetime(2025, 10, 23, tzinfo=UTC)
    result = _plan(
        lambda o, d: timedelta(minutes=40), current=[_block(dep, "airport_departure")], now=after
    )
    assert result.plan.deletes == ()


def test_a_trivial_leg_still_deletes_its_block():
    """A deliberate suppression is not a hold."""
    dep = _identity_of("airport_departure")
    result = _plan(lambda o, d: timedelta(seconds=30), current=[_block(dep, "airport_departure")])
    assert [d.event_id for d in result.plan.deletes] == ["evt-1"]


def test_airports_route_by_their_coordinates():
    seen: list = []

    def route(origin, destination):
        seen.append((origin, destination))
        return timedelta(minutes=40)

    result = _plan(route, current=[])
    assert (HOME, "36.12,-86.68") in seen
    assert any(o == "37.62,-122.38" for o, _ in seen)
    dep = next(c.desired for c in result.plan.creates if c.desired.kind == "airport_departure")
    assert dep.destination == "BNA airport"


def test_an_airport_without_coordinates_routes_by_name():
    seen: list = []

    def route(origin, destination):
        seen.append(destination)
        return timedelta(minutes=40)

    _plan(route, current=[], info={"BNA": AirportInfo(flag=US), "SFO": AirportInfo(flag=US)})
    assert "BNA airport" in seen


# --- the router ---------------------------------------------------------------


def _tt(source, minutes=40, origin_resolved="Airport, MO"):
    return TravelTime(
        duration_seconds=minutes * 60,
        in_traffic_seconds=None,
        traffic_factor=1.0,
        distance_meters=1000,
        origin_resolved=origin_resolved,
        destination_resolved="250 Beach St",
        source=source,
    )


class _Maps:
    def __init__(self, answer):
        self.answer = answer

    def travel_time(self, origin, destination):
        return self.answer


def test_a_backup_answer_for_a_text_airport_is_refused(capsys):
    route = reconcile_sweep.make_route(_Maps(_tt("tomtom", 298)))
    assert route("SFO airport", "250 Beach St, San Francisco") is None
    assert "refused" in capsys.readouterr().err


def test_a_backup_answer_for_coordinates_or_addresses_stands():
    route = reconcile_sweep.make_route(_Maps(_tt("tomtom")))
    assert route("37.62,-122.38", "250 Beach St") == timedelta(minutes=40)
    assert route(HOME, "250 Beach St") == timedelta(minutes=40)


def test_the_primary_answer_for_a_text_airport_stands():
    route = reconcile_sweep.make_route(_Maps(_tt("google")))
    assert route("SFO airport", HOME) == timedelta(minutes=40)


def test_tomtom_takes_coordinates_without_geocoding(monkeypatch):
    client = TomTomClient("key")
    monkeypatch.setattr(client, "_geocode", lambda query: pytest.fail(f"geocoded {query}"))
    assert client._locate("36.12,-86.68") == (36.12, -86.68, "36.12,-86.68")
    assert client._locate(" 36.12, -86.68 ") == (36.12, -86.68, "36.12, -86.68")


def test_tomtom_geocodes_a_name_or_an_out_of_range_pair(monkeypatch):
    client = TomTomClient("key")
    monkeypatch.setattr(client, "_geocode", lambda query: (1.0, 2.0, query))
    assert client._locate("SFO airport") == (1.0, 2.0, "SFO airport")
    assert client._locate("95,10") == (1.0, 2.0, "95,10")


def test_byair_coordinates_reach_the_airport_context():
    ctx = airport_context({"code": "BNA", "lat": 36.125247, "lon": -86.677102})
    assert (ctx.lat, ctx.lon) == (36.125247, -86.677102)
    bad = airport_context({"code": "BNA", "lat": "36.1", "lon": 400})
    assert (bad.lat, bad.lon) == (None, None)


# --- meeting and lodging legs --------------------------------------------------


def _meeting(origin=HOME):
    start = NOW + timedelta(days=1)
    return MeetingClass(
        meeting_id="m1",
        summary="Dentist",
        bucket="needs_decision",
        reason="standalone",
        location="1 Main St",
        start=start,
        end=start + timedelta(hours=1),
        legs=(
            TransitLeg(
                direction="outbound", origin=origin, destination="1 Main St", arrive_by=start
            ),
        ),
    )


def test_a_meeting_leg_that_cannot_route_is_held():
    blocks, _skipped, held = meeting_desired_blocks([_meeting()], route=lambda o, d: None)
    assert blocks == []
    assert [(leg.identity, leg.kind) for leg in held] == [("m1", "meeting_outbound")]
    assert held[0].summary == "Drive: Dentist"


def test_a_meeting_at_its_own_origin_is_dropped_not_held():
    _blocks, _skipped, held = meeting_desired_blocks(
        [_meeting(origin="1 Main St")], route=lambda o, d: timedelta(minutes=10)
    )
    assert held == []


def _trip():
    check_in = NOW + timedelta(days=2)
    return DriveTrip(
        key="trip-1",
        summary="Lake weekend",
        hotel="Lodge",
        address="9 Lake Rd",
        check_in=check_in,
        check_out=check_in + timedelta(days=2),
        span_start=check_in,
        span_end=check_in + timedelta(days=2),
        expires=check_in + timedelta(days=4),
    )


def test_a_lodging_trip_that_cannot_route_holds_both_legs():
    blocks, _skipped, _plans, held = lodging_desired_blocks(
        [_trip()], route=lambda o, d: None, home_address=HOME, verdicts={}, now=NOW
    )
    assert blocks == []
    assert sorted(leg.kind for leg in held) == sorted([KIND_OUTBOUND, KIND_RETURN])


# --- the operator notice -------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    return tmp_path


def _held(when=NOW + timedelta(days=13), identity="BNA-LGA-20251102T1840Z"):
    return HeldLeg(
        identity=identity,
        kind="airport_departure",
        summary="Drive: → BNA",
        when=when,
        reason=f"departure {identity}: route failed",
    )


def test_a_missing_drive_is_announced_once_after_an_hour(state_dir):
    leg = _held()
    assert unplanned_notices.due_notices([leg], now=NOW) == []
    assert unplanned_notices.due_notices([leg], now=NOW + timedelta(minutes=30)) == []
    assert unplanned_notices.due_notices([leg], now=NOW + timedelta(minutes=60)) == [leg]
    assert unplanned_notices.due_notices([leg], now=NOW + timedelta(minutes=90)) == []


def test_a_drive_that_plans_again_is_news_when_it_fails_again(state_dir):
    leg = _held()
    unplanned_notices.due_notices([leg], now=NOW)
    unplanned_notices.due_notices([leg], now=NOW + timedelta(hours=1))
    unplanned_notices.due_notices([], now=NOW + timedelta(hours=2))
    assert unplanned_notices.due_notices([leg], now=NOW + timedelta(hours=3)) == []
    assert unplanned_notices.due_notices([leg], now=NOW + timedelta(hours=4)) == [leg]


def test_a_drive_beyond_the_horizon_or_past_is_not_announced(state_dir):
    far = _held(when=NOW + timedelta(days=60))
    past = _held(when=NOW - timedelta(hours=1), identity="past")
    for hour in (0, 1, 2):
        assert unplanned_notices.due_notices([far, past], now=NOW + timedelta(hours=hour)) == []


def test_an_unreadable_notice_file_starts_fresh_and_says_so(state_dir, capsys):
    (state_dir / "unplanned-notices.v1.json").write_text("{nope", encoding="utf-8")
    assert unplanned_notices.due_notices([_held()], now=NOW) == []
    assert "unreadable" in capsys.readouterr().err
    stored = json.loads((state_dir / "unplanned-notices.v1.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1 and len(stored["legs"]) == 1


def test_only_held_legs_without_a_block_are_missing():
    kept, missing = _held(identity="kept"), _held(identity="missing")
    current = [_block("kept", "airport_departure")]
    assert reconcile_sweep._missing([kept, missing], current) == [missing]


def test_the_notice_line_reads_in_the_operator_zone():
    line = reconcile_sweep._unplanned_line(
        _held(when=datetime(2025, 11, 2, 17, 40, tzinfo=UTC)), "America/Chicago"
    )
    assert line == (
        "Couldn't plan Drive: → BNA (Sun Nov 02, 11:40): route failed. "
        "Check the flight in byAir, or add the drive to the calendar yourself."
    )


@pytest.mark.parametrize(
    ("kind", "check"),
    [
        ("meeting_return", "the event's location and time"),
        ("lodging_outbound", "the stay's address in TripIt"),
    ],
)
def test_the_notice_line_says_what_to_check(kind, check):
    leg = HeldLeg(identity="x", kind=kind, summary="Drive: X", when=None, reason="r: no route")
    assert reconcile_sweep._unplanned_line(leg, None) == (
        f"Couldn't plan Drive: X: no route. Check {check}, "
        "or add the drive to the calendar yourself."
    )


def test_unplanned_lines_reach_the_rendered_notice():
    applied = types.SimpleNamespace(
        added_meeting_legs=[],
        material_updates=[],
        created=0,
        updated=0,
        deleted=0,
        converted=0,
        deferred=0,
        errors=[],
    )
    payload = reconcile_sweep.build_sweep_payload(applied, [], [], ["Couldn't plan X: why"])
    assert payload["data"]["message"] == "Couldn't plan X: why"
    assert payload["data"]["unplanned"] == ["Couldn't plan X: why"]


def test_a_notice_store_that_cannot_be_written_costs_only_the_notice(monkeypatch, capsys):
    def broken(_missing, *, now):
        raise OSError("read-only file system")

    monkeypatch.setattr(reconcile_sweep, "due_notices", broken)
    assert reconcile_sweep._unplanned_notices((_held(),), [], now=NOW) == []
    assert "no unplanned-drive notice" in capsys.readouterr().err


def test_desired_block_type_is_unchanged_by_a_hold():
    """A held key never suppresses a desired leg of the same key."""
    block = DesiredBlock(
        identity="a",
        kind="airport_departure",
        summary="Drive: → BNA",
        start=NOW,
        end=NOW + timedelta(minutes=40),
        origin=HOME,
        destination="BNA airport",
        baseline_seconds=2400,
        anchor=NOW + timedelta(minutes=40),
    )
    plan = plan_reconcile([block], [], held=frozenset({("a", "airport_departure")}))
    assert len(plan.creates) == 1


def test_a_flown_trip_holds_nothing_when_it_cannot_be_planned():
    """An operator `fly` answer means no drive exists to keep."""
    from drive_decision import TripVerdict

    flown = {
        "trip-1": TripVerdict(
            verdict="fly",
            decided_by="operator",
            drive_seconds=None,
            asked_at=None,
            expires=NOW + timedelta(days=10),
        )
    }
    for home in (None, HOME):
        _blocks, skipped, _plans, held = lodging_desired_blocks(
            [_trip()], route=lambda o, d: None, home_address=home, verdicts=flown, now=NOW
        )
        assert held == [] and "flying, nothing held" in skipped[0]


def test_a_shadow_sweep_records_no_notice(state_dir, monkeypatch):
    monkeypatch.setenv("DRIVE_ENGINE_SHADOW", "1")
    leg = _held()
    for minutes in (0, 60):
        payload = reconcile_sweep.finish_sweep(
            reconcile_sweep.ReconcilePlan(),
            [],
            calendar=None,
            apply=lambda *a, **k: pytest.fail("a shadow sweep applied"),
            held=(leg,),
            current_blocks=[],
            now=NOW + timedelta(minutes=minutes),
        )
        assert payload["data"]["shadow"] is True
    assert not (state_dir / "unplanned-notices.v1.json").exists()


def test_a_live_sweep_announces_a_drive_missing_for_an_hour(state_dir, monkeypatch):
    monkeypatch.delenv("DRIVE_ENGINE_SHADOW", raising=False)
    applied = types.SimpleNamespace(
        added_meeting_legs=[],
        material_updates=[],
        created=0,
        updated=0,
        deleted=0,
        converted=0,
        deferred=0,
        errors=[],
    )
    leg = _held()
    messages = [
        reconcile_sweep.finish_sweep(
            reconcile_sweep.ReconcilePlan(),
            [],
            calendar=None,
            apply=lambda *a, **k: applied,
            held=(leg,),
            current_blocks=[],
            display_zone="America/Chicago",
            now=NOW + timedelta(minutes=minutes),
        )["data"]["message"]
        for minutes in (0, 60)
    ]
    assert messages[0] is None
    assert messages[1] is not None and messages[1].startswith("Couldn't plan Drive: → BNA")


def test_a_meeting_under_way_keeps_its_drives():
    """#336 review: a meeting that started is bucketed `past` with no legs; its
    return drive may still be ahead, so both its blocks are held."""
    start = NOW - timedelta(minutes=30)
    under_way = MeetingClass(
        meeting_id="m2",
        summary="Workshop",
        bucket="past",
        reason="meeting already started",
        location="1 Main St",
        start=start,
        end=start + timedelta(hours=2),
        legs=(),
    )
    _blocks, _skipped, held = meeting_desired_blocks([under_way], route=lambda o, d: None)
    assert {leg.key: leg.when for leg in held} == {
        ("m2", "meeting_outbound"): start,
        ("m2", "meeting_return"): start + timedelta(hours=2),
    }


def test_a_declined_meeting_holds_nothing():
    declined = MeetingClass(
        meeting_id="m3",
        summary="Lunch",
        bucket="filtered",
        reason="operator declined the meeting",
        location="1 Main St",
        start=NOW,
        end=NOW + timedelta(hours=1),
        legs=(),
    )
    assert meeting_desired_blocks([declined], route=lambda o, d: None)[2] == []


def test_a_past_block_is_history_not_an_orphan():
    past = _block("old", "meeting_return", "past-evt")
    past = ParsedBlock(**{**past.__dict__, "anchor": NOW - timedelta(days=1)})
    future = _block("gone", "meeting_return", "future-evt")
    plan = plan_reconcile([], [past, future], now=NOW)
    assert [d.event_id for d in plan.deletes] == ["future-evt"]


@pytest.mark.parametrize("version", [[], {}, "2", True])
def test_a_malformed_cache_version_reads_as_no_cache(tmp_path, monkeypatch, version, capsys):
    import airport_facts_cache

    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    (tmp_path / "airport-facts.json").write_text(
        json.dumps({"schema_version": version, "airports": {"3": {"iata": "JFK"}}}),
        encoding="utf-8",
    )
    assert airport_facts_cache.load_static_facts() == {}
    assert "refetching" in capsys.readouterr().err


# --- review round 3 (#336) ----------------------------------------------------


def _byair_record(scheduled_dep_time, fid=7):
    return {
        "flight_id": fid,
        "dep_airport_id": 1,
        "arr_airport_id": 2,
        "scheduled_dep_time": scheduled_dep_time,
    }


def _sweep_plan(records, current, resolve=lambda _id: None):
    return reconcile_sweep.build_plan(
        flight_records=records,
        resolve_airport=resolve,
        meeting_blocks=[],
        current_blocks=current,
        route=lambda o, d: timedelta(minutes=40),
        now=NOW,
        home_address=HOME,
    )


def test_an_unreadable_byair_record_keeps_its_airport_block():
    """A byAir record whose airports do not resolve leaves the desired set; its
    block must not read as an orphan on that malformed answer."""
    identity = _identity_of("airport_departure")
    current = [_block(identity, "airport_departure")]
    result = _sweep_plan([_byair_record("2025-10-22T15:00:00Z")], current)
    assert result.plan.deletes == ()
    assert [leg.key for leg in result.held] == [(identity, "airport_departure")]


def test_a_byair_record_without_a_departure_keeps_every_airport_block():
    identity = _identity_of("airport_departure")
    current = [_block(identity, "airport_departure"), _block("m9", "meeting_return", "evt-2")]
    result = _sweep_plan([_byair_record(None)], current)
    assert [d.event_id for d in result.plan.deletes] == ["evt-2"]


def test_an_unreadable_record_of_another_flight_leaves_the_orphan_to_go():
    identity = _identity_of("airport_departure")
    current = [_block(identity, "airport_departure")]
    result = _sweep_plan([_byair_record("2025-10-25T15:00:00Z")], current)
    assert [d.event_id for d in result.plan.deletes] == ["evt-1"]


def test_another_versions_notice_file_is_neither_read_nor_written(state_dir):
    """A newer engine's record lives in its own file: this engine still
    announces a missing drive, once, and leaves that file byte-for-byte."""
    newer = '{"schema_version": 2, "legs": {"kept": "as is"}}'
    (state_dir / "unplanned-notices.v2.json").write_text(newer, encoding="utf-8")
    leg = _held()
    seen = [unplanned_notices.due_notices([leg], now=NOW + timedelta(hours=h)) for h in (0, 1, 2)]
    assert seen == [[], [leg], []]
    assert (state_dir / "unplanned-notices.v2.json").read_text(encoding="utf-8") == newer


def test_a_transfer_still_ahead_in_its_window_is_not_history():
    """The connection was dropped mid-layover: its transfer drive, anchored at
    the landing that opened the window, is still ahead and goes."""
    transfer = ParsedBlock(
        **{
            **_block("A|B", "airport_transfer", "transfer-evt").__dict__,
            "anchor": NOW - timedelta(hours=1),
            "window_end": NOW + timedelta(hours=3),
        }
    )
    done = ParsedBlock(
        **{
            **_block("C|D", "airport_transfer", "done-evt").__dict__,
            "anchor": NOW - timedelta(hours=5),
            "window_end": NOW - timedelta(hours=1),
        }
    )
    plan = plan_reconcile([], [transfer, done], now=NOW)
    assert [d.event_id for d in plan.deletes] == ["transfer-evt"]


def test_a_hold_that_only_keeps_blocks_is_never_missing():
    keeping = HeldLeg(
        identity="mtg-back",
        kind="meeting_return",
        summary="Drive: Game",
        when=NOW + timedelta(days=1),
        reason="lodging trip-1: lodging→home route failed",
        announce=False,
    )
    assert reconcile_sweep._missing([keeping], []) == []


@pytest.mark.parametrize("reason", ["meeting skipped by the operator", "meeting already started"])
def test_a_meeting_under_way_is_never_announced_missing(reason):
    """#336 review: a skipped meeting, or one whose return the scan left out
    across a tight gap, turns `past` once it starts; its holds only keep blocks."""
    start = NOW - timedelta(minutes=30)
    under_way = MeetingClass(
        meeting_id="m4",
        summary="Workshop",
        bucket="past",
        reason=reason,
        location="1 Main St",
        start=start,
        end=start + timedelta(hours=4),
        legs=(),
    )
    _blocks, _skipped, held = meeting_desired_blocks([under_way], route=lambda o, d: None)
    assert held and reconcile_sweep._missing(held, []) == []


def test_a_notice_file_without_legs_starts_fresh_and_says_so(state_dir, capsys):
    path = state_dir / "unplanned-notices.v1.json"
    path.write_text('{"schema_version": 1}', encoding="utf-8")
    assert unplanned_notices.due_notices([_held()], now=NOW) == []
    assert f"{path} has no `legs` object" in capsys.readouterr().err


def test_tripits_copy_of_a_held_byair_flight_plans_nothing_beside_it():
    """#336 review: byAir's block says 15:00, TripIt's copy 14:55. With the byAir
    record unreadable, the held block stands for the journey, and TripIt's
    copy must not create a second drive under its own identity."""
    identity = _identity_of("airport_departure")
    current = [_block(identity, "airport_departure")]
    tripit = Flight(
        dep_airport="BNA",
        arr_airport="SFO",
        scheduled_dep=datetime(2025, 10, 22, 14, 55, tzinfo=UTC),
        scheduled_arr=datetime(2025, 10, 22, 18, 55, tzinfo=UTC),
        source=TRIPIT,
        tripit_segment_id="seg-1",
    )
    result = reconcile_sweep.build_plan(
        flight_records=[_byair_record("2025-10-22T15:00:00Z")],
        resolve_airport=lambda _id: None,
        meeting_blocks=[],
        current_blocks=current,
        route=lambda o, d: timedelta(minutes=40),
        now=NOW,
        home_address=HOME,
        tripit_flights=[tripit],
    )
    # The departure block is held under byAir's 15:00 key; TripIt's 14:55 copy
    # writes no second departure. Its arrival has no block yet and plans.
    assert [c.desired.kind for c in result.plan.creates] == ["airport_arrival"]
    assert result.plan.deletes == ()


def test_tripits_copy_plans_when_no_block_is_held_for_it():
    tripit = Flight(
        dep_airport="BNA",
        arr_airport="SFO",
        scheduled_dep=datetime(2025, 10, 22, 14, 55, tzinfo=UTC),
        scheduled_arr=datetime(2025, 10, 22, 18, 55, tzinfo=UTC),
        source=TRIPIT,
        tripit_segment_id="seg-1",
    )
    result = reconcile_sweep.build_plan(
        flight_records=[_byair_record("2025-10-22T15:00:00Z")],
        resolve_airport=lambda _id: None,
        meeting_blocks=[],
        current_blocks=[],
        route=lambda o, d: timedelta(minutes=40),
        now=NOW,
        home_address=HOME,
        tripit_flights=[tripit],
    )
    assert result.plan.creates != ()


# --- review round 4 (#337) ----------------------------------------------------


def _status_flight(status, dep="BNA", arr="SFO", day=22):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=datetime(2025, 10, day, 15, 0, tzinfo=UTC),
        scheduled_arr=datetime(2025, 10, day, 19, 0, tzinfo=UTC),
        source=BYAIR,
        byair_flight_id=day,
        status=status,
    )


def test_a_cancelled_flight_plans_no_drive():
    result = _plan(
        lambda o, d: timedelta(minutes=40), current=[], flights=[_status_flight("cancelled")]
    )
    assert result.plan.creates == ()


def test_a_diverted_flight_plans_no_drive_from_its_booked_destination():
    result = _plan(
        lambda o, d: timedelta(minutes=40), current=[], flights=[_status_flight("diverted")]
    )
    assert [c.desired.kind for c in result.plan.creates] == ["airport_departure"]


def test_an_unreadable_record_with_times_is_placed_at_an_unknown_airport():
    record = {
        **_byair_record("2025-10-22T15:00:00Z"),
        "scheduled_arr_time": "2025-10-22T19:00:00Z",
    }
    flights = reconcile_sweep.normalize_flights([record], lambda _id: None)
    (transport,) = flights.unplaced
    assert (transport.origin, transport.destination) == (None, None)
    assert flights.blind == ()


def test_an_unreadable_record_tripit_covers_is_not_placed_twice():
    record = {
        **_byair_record("2025-10-22T15:00:00Z"),
        "scheduled_arr_time": "2025-10-22T19:00:00Z",
    }
    tripit = Flight(
        dep_airport="BNA",
        arr_airport="SFO",
        scheduled_dep=datetime(2025, 10, 22, 14, 55, tzinfo=UTC),
        source=TRIPIT,
        tripit_segment_id="seg-1",
    )
    assert reconcile_sweep.normalize_flights([record], lambda _id: None, [tripit]).unplaced == ()


def test_a_record_with_no_readable_times_fails_the_sweep_closed():
    flights = reconcile_sweep.normalize_flights([_byair_record(None)], lambda _id: None)
    with pytest.raises(reconcile_sweep.FlightsUnplaceable, match="fix or remove"):
        reconcile_sweep.require_placeable(flights)
