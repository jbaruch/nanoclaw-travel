"""Tests for the live precheck core (build_plan).

Deterministic fixtures only — hand-built byAir records, pre-built meeting blocks,
a fake airport resolver and router, fixed `now`. These pin: airport + meeting
blocks are combined into ONE plan; legacy drive-planner (dp) blocks on the calendar
are LEFT UNTOUCHED (managed_legacy empty — the operator cleans them up); an
unresolvable airport is skipped, not guessed. main()'s live I/O work (`_run_sweep`)
is not unit-tested, but main()'s outer-boundary contract — generic error payload vs.
work payload — is, via a monkeypatched `_run_sweep`. The airport-resolution policy
(`_resolve_one_airport`) and near-term delay selection are unit-tested directly (#211).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

import pytest  # noqa: E402
import reconcile_sweep  # noqa: E402
from airport_facts_cache import StaticAirport  # noqa: E402
from block_codec import GEN_LEGACY_DP, ParsedBlock  # noqa: E402
from flight_identity import TRIPIT, Flight  # noqa: E402
from maps_client import MapsError, TravelTime  # noqa: E402
from reconcile import Create, Delete, DesiredBlock, ReconcilePlan  # noqa: E402
from reconcile_sweep import (  # noqa: E402
    AirportUnresolved,
    ResolvedAirport,
    _latest_itinerary_instant,
    _make_airport_resolver,
    _near_term_departure_airport_ids,
    _persist_static_facts_best_effort,
    _resolve_one_airport,
    build_plan,
    make_route,
)

UTC = UTC
HOME = "12 Example St, TN"
NOW = datetime(2020, 7, 10, 12, 0, tzinfo=UTC)
US = "🇺🇸"

_IATA = {3: "JFK", 4: "BNA"}


def _resolve_airport(airport_id):
    iata = _IATA.get(airport_id)
    return None if iata is None else ResolvedAirport(iata=iata, flag=US, delay_index="low")


def _route(_o, _d):
    return timedelta(minutes=30)


def _record(fid, dep_id, arr_id, dep, arr):
    return {
        "schema_version": 6,
        "flight_id": fid,
        "code": "AA1",
        "trip_id": 7,
        "scheduled_dep_time": dep,
        "scheduled_arr_time": arr,
        "dep_airport_id": dep_id,
        "arr_airport_id": arr_id,
        "last_snapshot": None,
    }


def _meeting_block(identity="mtg1"):
    a = datetime(2020, 7, 12, 15, tzinfo=UTC)
    return DesiredBlock(
        identity=identity,
        kind="meeting_outbound",
        summary="Drive: Offsite",
        start=a - timedelta(minutes=30),
        end=a,
        origin="Home",
        destination="Venue",
        baseline_seconds=1800,
        anchor=a,
        timezone="America/Chicago",
    )


def _tripit_flight(dep, arr, sdep, sarr, *, seg="seg-1", trip_id=7):
    return Flight(
        dep_airport=dep,
        arr_airport=arr,
        scheduled_dep=datetime.fromisoformat(sdep),
        scheduled_arr=datetime.fromisoformat(sarr),
        code="AA1",
        source=TRIPIT,
        tripit_segment_id=seg,
        trip_id=trip_id,
    )


def test_tripit_only_flight_is_unioned_and_produces_legs():
    # A flight byAir never tracked (no records) still yields airport legs via the
    # TripIt union (R2). Its airports have no byAir facts — degraded but present.
    tf = _tripit_flight("ATL", "SJO", "2020-07-12T09:00:00+00:00", "2020-07-12T14:30:00+00:00")
    result = build_plan(
        flight_records=[],
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
        tripit_flights=[tf],
    )
    kinds = sorted(c.desired.kind for c in result.plan.creates)
    assert "airport_departure" in kinds and "airport_arrival" in kinds


def test_tripit_only_connection_groups_no_interior_legs():
    # Two TripIt-only legs of ONE trip (shared trip_id) with a same-airport
    # connection (CPH) must be recognized as a connection — only the opening
    # departure (STN) and closing arrival (JFK), NOT independent per-leg drives.
    legs = [
        _tripit_flight(
            "STN",
            "CPH",
            "2020-07-12T09:00:00+00:00",
            "2020-07-12T11:00:00+00:00",
            seg="s1",
            trip_id=-98765,
        ),
        _tripit_flight(
            "CPH",
            "JFK",
            "2020-07-12T13:00:00+00:00",
            "2020-07-12T20:00:00+00:00",
            seg="s2",
            trip_id=-98765,
        ),
    ]
    result = build_plan(
        flight_records=[],
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
        tripit_flights=legs,
    )
    created = {(c.desired.kind, c.desired.destination) for c in result.plan.creates}
    assert created == {("airport_departure", "STN airport"), ("airport_arrival", HOME)}


def test_boarding_present_gates_trivial_suppression():
    # A trivial airport drive is suppressed only when a boarding block exists (V3).
    records = [_record(1, 4, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]

    def trivial_route(_o, _d):
        return timedelta(minutes=5)  # <= trivial threshold

    with_boarding = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=trivial_route,
        now=NOW,
        home_address=HOME,
        boarding_present=lambda _f: True,
    )
    without_boarding = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=trivial_route,
        now=NOW,
        home_address=HOME,
        boarding_present=lambda _f: False,
    )
    dep_with = [
        c.desired.kind for c in with_boarding.plan.creates if c.desired.kind == "airport_departure"
    ]
    dep_without = [
        c.desired.kind
        for c in without_boarding.plan.creates
        if c.desired.kind == "airport_departure"
    ]
    assert dep_with == []  # boarding present → trivial departure suppressed
    assert dep_without == ["airport_departure"]  # no boarding block → kept


def test_combines_airport_and_meeting_blocks():
    records = [_record(1, 4, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[_meeting_block()],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    kinds = sorted(c.desired.kind for c in result.plan.creates)
    # a single BNA->JFK flight yields departure + arrival; plus the meeting
    assert "meeting_outbound" in kinds
    assert "airport_departure" in kinds and "airport_arrival" in kinds


def test_legacy_dp_blocks_left_untouched():
    # An existing drive-planner meeting block must NOT be deleted or converted —
    # the operator cleans those up; the engine only manages its own blocks.
    dp = ParsedBlock(
        generation=GEN_LEGACY_DP,
        event_id="dp-swim",
        legacy_id="mtg-swim",
        legacy_direction="outbound",
    )
    result = build_plan(
        flight_records=[],
        resolve_airport=_resolve_airport,
        meeting_blocks=[_meeting_block()],
        current_blocks=[dp],
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    assert all(d.event_id != "dp-swim" for d in result.plan.deletes)
    assert result.plan.deletes == ()  # nothing deleted
    assert any(c.desired.kind == "meeting_outbound" for c in result.plan.creates)


def test_no_home_off_trip_degrades_not_crashes():
    # With no home_address and no active trip, the airport side must degrade (legs
    # skipped with a diagnostic) rather than raise — a missing home never takes the
    # sweep down (#162). The meeting side is guarded separately in main().
    records = [_record(1, 4, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=None,  # neither config nor user_profile provided one
    )
    assert result.plan.creates == ()  # no origin → no blind block
    assert any("unresolved" in s for s in result.skipped)


def test_unresolved_airport_skipped():
    records = [_record(1, 9, 3, "2020-07-12T09:00:00Z", "2020-07-12T11:00:00Z")]
    result = build_plan(
        flight_records=records,
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
    )
    assert result.plan.is_noop
    assert any("unresolved airport" in s for s in result.skipped)


# --- make_route memoization (#172) ------------------------------------------


class _FakeMaps:
    """Counts travel_time calls and can be told to fail, to pin memoization."""

    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    def travel_time(self, origin: str, destination: str) -> TravelTime:
        self.calls.append((origin, destination))
        if self._fail:
            raise MapsError("ALL_PROVIDERS_FAILED", "boom")
        return TravelTime(
            duration_seconds=1800,
            in_traffic_seconds=1800,
            traffic_factor=1.0,
            distance_meters=1000,
            origin_resolved=origin,
            destination_resolved=destination,
            source="google",
        )


def test_make_route_memoizes_repeated_pair():
    """A repeated (origin, destination) pair — an airport that is both a departure
    destination and a transfer origin — routes ONCE, not per leg (#172)."""
    maps = _FakeMaps()
    route = make_route(maps)
    first = route("home", "STN airport")
    second = route("home", "STN airport")
    assert first == second == timedelta(seconds=1800)
    assert maps.calls == [("home", "STN airport")]  # one round trip, not two


def test_make_route_distinct_pairs_each_route_once():
    maps = _FakeMaps()
    route = make_route(maps)
    route("home", "STN airport")
    route("STN airport", "CPH airport")
    route("home", "STN airport")  # repeat of the first
    assert maps.calls == [("home", "STN airport"), ("STN airport", "CPH airport")]


def test_make_route_caches_failure_as_none():
    """A dead endpoint caches None so it isn't re-attempted every leg (each retry
    is the same slow provider-failover that caused the storm) (#172)."""
    maps = _FakeMaps(fail=True)
    route = make_route(maps)
    assert route("home", "STN airport") is None
    assert route("home", "STN airport") is None
    assert maps.calls == [("home", "STN airport")]  # failure not re-attempted


# --- airport resolution: static cache + near-term delay refresh (#211) -------


class _FakeCtx:
    """Stand-in for `airport_context`'s AirportContext (the `_AirportCtx`
    Protocol shape)."""

    def __init__(self, code, flag=None, delay_index=None, timezone=None, lat=None, lon=None):
        self.code = code
        self.flag = flag
        self.delay_index = delay_index
        self.timezone = timezone
        self.lat = lat
        self.lon = lon


def test_resolve_warm_static_hit_no_delay_never_fetches():
    """A cached airport with no near-term departure resolves with ZERO byAir
    calls — the warm-sweep saving that unfroze the calendar (#211)."""
    static = StaticAirport(
        iata="JFK", flag="🇺🇸", timezone="America/New_York", lat=40.64, lon=-73.78
    )
    calls = []

    def fetch():
        calls.append(1)
        return _FakeCtx("JFK")

    resolved, new_static = _resolve_one_airport(static=static, want_delay=False, fetch=fetch)
    assert calls == []  # never fetched
    assert resolved == ResolvedAirport(
        iata="JFK", flag="🇺🇸", timezone="America/New_York", lat=40.64, lon=-73.78
    )
    assert new_static is None  # nothing new to persist


def test_resolve_a_cached_airport_without_coordinates_refetches_them():
    """A version-1 cache entry has no coordinates; one fetch fills them (#334)."""
    static = StaticAirport(iata="JFK", flag="🇺🇸", timezone="America/New_York")
    fetched = _FakeCtx("JFK", flag="🇺🇸", timezone="America/New_York", lat=40.64, lon=-73.78)
    resolved, new_static = _resolve_one_airport(
        static=static, want_delay=False, fetch=lambda: fetched
    )
    assert (resolved.lat, resolved.lon) == (40.64, -73.78)
    assert new_static is not None and new_static.has_coordinates


def test_resolve_a_coordinate_refetch_that_fails_keeps_the_cached_facts():
    static = StaticAirport(iata="JFK", flag="🇺🇸", timezone="America/New_York")
    resolved, new_static = _resolve_one_airport(static=static, want_delay=False, fetch=lambda: None)
    assert (resolved.iata, resolved.lat, new_static) == ("JFK", None, None)


def test_resolve_miss_fetches_and_returns_static_to_persist():
    """A first-seen airport fetches, resolves, and hands back the static trio to
    persist (never the live delay index)."""
    resolved, new_static = _resolve_one_airport(
        static=None,
        want_delay=False,
        fetch=lambda: _FakeCtx("BNA", flag="🇺🇸", delay_index="high", timezone="America/Chicago"),
    )
    assert resolved == ResolvedAirport(iata="BNA", flag="🇺🇸", timezone="America/Chicago")
    assert new_static == StaticAirport(iata="BNA", flag="🇺🇸", timezone="America/Chicago")


def test_resolve_near_term_refreshes_delay_but_persists_only_static():
    """A near-term departure (want_delay) fetches to carry the fresh delay index
    on the resolved airport, yet the persisted static trio still omits it."""
    static = StaticAirport(iata="SFO", flag="🇺🇸", timezone="America/Los_Angeles")
    ctx = _FakeCtx("SFO", flag="🇺🇸", delay_index="high", timezone="America/Los_Angeles")
    resolved, new_static = _resolve_one_airport(static=static, want_delay=True, fetch=lambda: ctx)
    assert resolved is not None and resolved.delay_index == "high"  # live nudge on the block
    assert new_static is None  # static unchanged → nothing re-persisted


def test_resolve_byair_failure_falls_back_to_cached_static():
    """A byAir failure on a near-term refresh degrades to the cached static facts
    (delay dropped) rather than skipping the flight."""
    static = StaticAirport(iata="LHR", flag="🇬🇧", timezone="Europe/London")
    resolved, new_static = _resolve_one_airport(static=static, want_delay=True, fetch=lambda: None)
    assert resolved == ResolvedAirport(iata="LHR", flag="🇬🇧", timezone="Europe/London")
    assert new_static is None


def test_resolve_miss_with_byair_failure_raises_fail_closed():
    """A first-seen airport whose byAir fetch fails RAISES `AirportUnresolved` —
    never a None that would drop the flight and orphan-delete its block. The whole
    sweep fails closed and retries next cycle (#211 review)."""
    with pytest.raises(AirportUnresolved):
        _resolve_one_airport(static=None, want_delay=False, fetch=lambda: None)


def test_resolve_none_iata_context_with_static_degrades_to_cache():
    """A code-less live context (byAir responded but carried no IATA) with cached
    static facts degrades to those facts — never a code-less ResolvedAirport that
    would drop the flight and orphan-delete its block (#213 review round 3)."""
    static = StaticAirport(iata="LHR", flag="🇬🇧", timezone="Europe/London")
    resolved, new_static = _resolve_one_airport(
        static=static, want_delay=True, fetch=lambda: _FakeCtx(None, flag="🇬🇧")
    )
    assert resolved == ResolvedAirport(iata="LHR", flag="🇬🇧", timezone="Europe/London")
    assert new_static is None  # nothing fresh to persist


def test_resolve_none_iata_context_no_static_raises():
    """A code-less live context with NO cached fallback fails the sweep closed,
    never a code-less resolution that would drop the flight into a partial plan."""
    with pytest.raises(AirportUnresolved):
        _resolve_one_airport(static=None, want_delay=False, fetch=lambda: _FakeCtx(None, flag="🇺🇸"))


def _counting_fetch(ctx_by_id):
    """A `fetch_ctx(airport_id)` that records calls and serves from `ctx_by_id`."""
    calls = []

    def fetch(airport_id):
        calls.append(airport_id)
        return ctx_by_id.get(airport_id)

    return fetch, calls


def test_resolver_memoizes_repeated_airport():
    """A repeated airport (both a departure origin and a transfer endpoint)
    resolves ONCE — one byAir round trip, not one per leg (#211)."""
    fetch, calls = _counting_fetch({5: _FakeCtx("SFO", timezone="America/Los_Angeles")})
    resolve, dirty = _make_airport_resolver(
        static_facts={}, near_term_dep_ids=set(), fetch_ctx=fetch
    )
    first = resolve(5)
    second = resolve(5)
    assert first == second == ResolvedAirport(iata="SFO", timezone="America/Los_Angeles")
    assert calls == [5]  # memoized — one fetch
    assert dirty() is True  # a fresh fact was learned


def test_resolver_cache_hit_never_skips_near_term_delay_refresh():
    """The order-independence invariant (the #211 review concern): `want_delay` is
    a pure function of airport_id, so a near-term departure's live delay is carried
    on the FIRST resolution regardless of which leg triggered it — a later cache
    hit can never serve a stale no-delay result. Resolving airport 5 (a near-term
    departure) twice yields the delay both times from a single fetch."""
    fetch, calls = _counting_fetch({5: _FakeCtx("SFO", delay_index="high")})
    resolve, _dirty = _make_airport_resolver(
        static_facts={5: StaticAirport(iata="SFO")},  # already statically cached
        near_term_dep_ids={5},  # ...but near-term, so delay must refresh
        fetch_ctx=fetch,
    )
    first = resolve(5)  # e.g. reached first as another flight's arrival endpoint
    second = resolve(5)  # then as its own near-term departure
    assert first is not None and first.delay_index == "high"
    assert second == first  # cache hit carries the same fresh delay
    assert calls == [5]  # exactly one byAir refresh, not zero and not two


def test_resolver_warm_hit_makes_no_fetch_and_stays_clean():
    """A statically-cached airport with no near-term departure resolves with zero
    fetches and leaves the dirty flag unset (nothing new to persist)."""
    fetch, calls = _counting_fetch({})
    resolve, dirty = _make_airport_resolver(
        static_facts={
            7: StaticAirport(
                iata="BNA", flag="🇺🇸", timezone="America/Chicago", lat=36.12, lon=-86.68
            )
        },
        near_term_dep_ids=set(),
        fetch_ctx=fetch,
    )
    assert resolve(7) == ResolvedAirport(
        iata="BNA", flag="🇺🇸", timezone="America/Chicago", lat=36.12, lon=-86.68
    )
    assert calls == []
    assert dirty() is False


def test_resolver_byair_miss_raises_fail_closed():
    """A first-seen airport whose fetch fails propagates `AirportUnresolved` out of
    the resolver — the sweep fails closed rather than dropping the flight and
    orphan-deleting its block (#211 review). No fact is persisted."""
    fetch, calls = _counting_fetch({})  # id 9 absent → fetch returns None
    resolve, dirty = _make_airport_resolver(
        static_facts={}, near_term_dep_ids=set(), fetch_ctx=fetch
    )
    with pytest.raises(AirportUnresolved):
        resolve(9)
    assert calls == [9]
    assert dirty() is False


def test_persist_facts_swallows_write_error_and_warns(monkeypatch, capsys):
    """A cache WRITE failure must not abort the sweep — the cache is a latency
    hint, so an OSError is logged and swallowed (never propagated to main()'s
    fail-closed catch, which would skip applying a valid plan) (#213 review)."""

    def boom(_facts):
        raise OSError("read-only file system")

    monkeypatch.setattr(reconcile_sweep, "store_static_facts", boom)
    _persist_static_facts_best_effort({})  # must not raise
    assert "could not persist airport-facts cache" in capsys.readouterr().err


def test_persist_facts_calls_store_on_happy_path(monkeypatch, capsys):
    saved = []
    monkeypatch.setattr(reconcile_sweep, "store_static_facts", lambda facts: saved.append(facts))
    _persist_static_facts_best_effort({3: StaticAirport(iata="JFK")})
    assert saved == [{3: StaticAirport(iata="JFK")}]
    assert capsys.readouterr().err == ""  # silent on success


def test_latest_itinerary_instant_none_when_no_flights():
    assert _latest_itinerary_instant([], []) is None


def test_latest_itinerary_instant_spans_both_sources_and_prefers_arrival():
    """The fetch horizon must reach the LAST flight instant across byAir records
    and TripIt flights — a far-future TripIt segment (months out) must win over a
    near-term byAir record, else its block sits beyond the window and dupes every
    sweep. Arrival wins over departure so a drive-home leg is covered."""
    records = [
        {
            "scheduled_dep_time": "2020-07-11T06:00:00+00:00",
            "scheduled_arr_time": "2020-07-11T09:00:00+00:00",
        },
    ]
    tripit = [
        _tripit_flight("BNA", "OSL", "2020-09-06T10:00:00+00:00", "2020-09-06T18:00:00+00:00"),
    ]
    assert _latest_itinerary_instant(records, tripit) == datetime(2020, 9, 6, 18, 0, tzinfo=UTC)


def test_latest_itinerary_instant_falls_back_to_departure_time():
    """A byAir record with only a (properly offset) departure time still counts."""
    records = [{"scheduled_dep_time": "2020-08-01T12:00:00+00:00"}]
    assert _latest_itinerary_instant(records, []) == datetime(2020, 8, 1, 12, 0, tzinfo=UTC)


def test_latest_itinerary_instant_skips_tz_naive_byair_time():
    """A tz-naive byAir timestamp is malformed (an offset is required) and is
    skipped, not coerced to UTC — matching `_near_term_departure_airport_ids`, so
    corrupted state can't silently stretch the fetch horizon."""
    assert _latest_itinerary_instant([{"scheduled_dep_time": "2020-08-01T12:00:00"}], []) is None


def test_near_term_departure_ids_selects_within_window_only():
    """Only byAir departures within `_DELAY_FRESHNESS_WINDOW` of now get a delay
    refresh; past, far-future, and unparseable ones are excluded (#211). Fixed
    past fixture dates relative to the injected `now` (per testing-standards)."""
    now = datetime(2020, 7, 10, 12, 0, tzinfo=UTC)
    records = [
        {"dep_airport_id": 1, "scheduled_dep_time": "2020-07-10T18:00:00+00:00"},  # in 6h → in
        {"dep_airport_id": 2, "scheduled_dep_time": "2020-07-13T12:00:00+00:00"},  # 3d → out
        {"dep_airport_id": 3, "scheduled_dep_time": "2020-07-10T06:00:00+00:00"},  # past → out
        {"dep_airport_id": 4, "scheduled_dep_time": "not-a-date"},  # unparseable → out
        {"scheduled_dep_time": "2020-07-10T13:00:00+00:00"},  # no dep id → out
    ]
    assert _near_term_departure_airport_ids(records, now) == {1}


def test_near_term_departure_ids_excludes_naive_timestamps():
    """A tz-naive `scheduled_dep_time` can't be safely compared to the aware now,
    so it is excluded rather than guessed."""
    now = datetime(2020, 7, 10, 12, 0, tzinfo=UTC)
    records = [{"dep_airport_id": 9, "scheduled_dep_time": "2020-07-10T13:00:00"}]
    assert _near_term_departure_airport_ids(records, now) == set()


# --- main() outer-boundary contract ------------------------------------------


def test_main_emits_error_payload_on_unexpected_exception(monkeypatch, capsys):
    """A non-budget failure still fails closed: a valid no-wake payload carrying
    the error (never a non-zero exit or empty stdout, which the scheduler reads as
    silent failure)."""
    monkeypatch.setattr(
        reconcile_sweep,
        "_run_sweep",
        lambda: (_ for _ in ()).throw(RuntimeError("kaboom")),
    )
    rc = reconcile_sweep.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["wake_agent"] is False and out["data"]["error"] == "kaboom"


def test_main_prints_run_sweep_payload_on_success(monkeypatch, capsys):
    """On a normal run main() prints exactly the payload _run_sweep returns."""
    payload = {"wake_agent": True, "data": {"applied": {"created": 1}}}
    monkeypatch.setattr(reconcile_sweep, "_run_sweep", lambda: payload)
    rc = reconcile_sweep.main()
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == payload


# --- shadow mode (#156 R4, wired in #183) --------------------------------


def _shadow_plan():
    a = datetime(2020, 7, 12, 8, tzinfo=UTC)
    desired = DesiredBlock(
        identity="STN-CPH-20200712T0900Z",
        kind="airport_departure",
        summary="Drive: STN-CPH",
        start=a,
        end=a,
        origin="Hotel",
        destination="APT",
        baseline_seconds=1800,
        anchor=a,
    )
    return ReconcilePlan(creates=(Create(desired),), deletes=(Delete("evt9", "legacy orphan"),))


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_shadow_mode_on_for_truthy_values(monkeypatch, value):
    monkeypatch.setenv("DRIVE_ENGINE_SHADOW", value)
    assert reconcile_sweep._shadow_mode() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_shadow_mode_off_for_everything_else(monkeypatch, value):
    monkeypatch.setenv("DRIVE_ENGINE_SHADOW", value)
    assert reconcile_sweep._shadow_mode() is False


def test_shadow_mode_off_when_unset(monkeypatch):
    """The scheduled sweep applies; shadow is opt-in only."""
    monkeypatch.delenv("DRIVE_ENGINE_SHADOW", raising=False)
    assert reconcile_sweep._shadow_mode() is False


def test_shadow_payload_never_wakes():
    """A dry run changed nothing, so there is nothing to wake the operator for."""
    payload = reconcile_sweep.build_shadow_payload(_shadow_plan(), [])
    assert payload["wake_agent"] is False


def test_shadow_payload_reports_planned_counts_not_applied():
    payload = reconcile_sweep.build_shadow_payload(_shadow_plan(), ["skipped a leg"])
    data = payload["data"]
    assert data["shadow"] is True
    assert data["planned"] == {
        "creates": 1,
        "updates": 0,
        "deletes": 1,
        "converts": 0,
        "legacy_converted": 0,
    }
    assert data["skipped"] == 1
    # a shadow run applied nothing, so it must not report `applied` counts
    assert "applied" not in data


def test_shadow_payload_is_json_serializable():
    """main() prints the payload with json.dumps — an unserializable value would
    break the scheduler's stdout contract."""
    payload = reconcile_sweep.build_shadow_payload(_shadow_plan(), [])
    assert json.loads(json.dumps(payload))["data"]["shadow"] is True


# --- the shadow branch's safety contract (#183) --------------------------
#
# `finish_sweep` is the seam `_run_sweep` delegates the shadow-vs-apply
# decision to, so the no-write guarantee is testable without live clients.


class _ApplySpy:
    """Stands in for `apply_plan`; records whether it was ever called."""

    def __init__(self):
        self.calls = []

    def __call__(self, plan, **kwargs):
        self.calls.append((plan, kwargs))
        return _ApplyResult()


class _ApplyResult:
    skipped_creations = ()
    created = updated = deleted = converted = 0
    added_meeting_legs = ()
    material_updates = ()
    deferred = 0
    errors = ()


def test_shadow_branch_never_calls_apply(monkeypatch, capsys):
    """The safety contract: a shadow run must not touch the calendar."""
    monkeypatch.setenv("DRIVE_ENGINE_SHADOW", "1")
    spy = _ApplySpy()
    reconcile_sweep.finish_sweep(_shadow_plan(), [], calendar=object(), apply=spy)
    assert spy.calls == []


def test_shadow_branch_renders_to_stderr_not_stdout(monkeypatch, capsys):
    """stdout carries the scheduler's JSON payload — the diff must not land there."""
    monkeypatch.setenv("DRIVE_ENGINE_SHADOW", "1")
    reconcile_sweep.finish_sweep(_shadow_plan(), [], calendar=object(), apply=_ApplySpy())
    out, err = capsys.readouterr()
    assert "[shadow] reconcile plan" in err
    assert "+ CREATE airport_departure" in err
    assert out == ""


def test_shadow_branch_returns_the_shadow_payload(monkeypatch):
    monkeypatch.setenv("DRIVE_ENGINE_SHADOW", "1")
    payload = reconcile_sweep.finish_sweep(
        _shadow_plan(), ["a skip"], calendar=object(), apply=_ApplySpy()
    )
    assert payload["wake_agent"] is False
    assert payload["data"]["shadow"] is True
    assert payload["data"]["planned"]["creates"] == 1


def test_live_branch_applies_when_shadow_is_off(monkeypatch):
    """The scheduled sweep is untouched: shadow off → apply is called as before."""
    monkeypatch.delenv("DRIVE_ENGINE_SHADOW", raising=False)
    spy = _ApplySpy()
    cal = object()
    payload = reconcile_sweep.finish_sweep(_shadow_plan(), [], calendar=cal, apply=spy)
    assert len(spy.calls) == 1
    plan, kwargs = spy.calls[0]
    assert kwargs["calendar"] is cal and kwargs["calendar_id"] == "primary"
    assert "shadow" not in payload["data"]


def test_live_branch_passes_fixed_apply_budget(monkeypatch):
    """#211: apply gets a FIXED write-phase budget, decoupled from how long the
    plan phase took — never the old `budget - elapsed` that starved it to 0."""
    monkeypatch.delenv("DRIVE_ENGINE_SHADOW", raising=False)
    spy = _ApplySpy()
    reconcile_sweep.finish_sweep(_shadow_plan(), [], calendar=object(), apply=spy)
    assert spy.calls[0][1]["budget_seconds"] == reconcile_sweep._APPLY_PHASE_BUDGET_SECONDS


def test_live_branch_can_apply_without_a_write_budget(monkeypatch):
    """An operator can opt one repair sweep into draining the complete plan."""
    monkeypatch.delenv("DRIVE_ENGINE_SHADOW", raising=False)
    monkeypatch.setenv("DRIVE_ENGINE_UNBOUNDED_APPLY", "1")
    spy = _ApplySpy()
    reconcile_sweep.finish_sweep(_shadow_plan(), [], calendar=object(), apply=spy)
    assert spy.calls[0][1]["budget_seconds"] is None


# ---------------------------------------------------------------------------
# Lodging legs absorbing a local drive (#231 follow-up)
# ---------------------------------------------------------------------------


def _drive_trip_schedule():
    """A flight-less trip with lodging, in `travel-schedule.json`'s own shape."""
    return [
        {
            "type": "Trip",
            "summary": "Tigers Weekend",
            "start": "2020-08-14",
            "end": "2020-08-16",
            "location": "TN",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Fairfield Inn",
            "start": "2020-08-14T20:00:00Z",
            "end": "2020-08-14T21:00:00Z",
            "location": "611 Historic Nature Trail Gatlinburg TN 37738 US",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Fairfield Inn",
            "start": "2020-08-15T15:00:00Z",
            "end": "2020-08-15T16:00:00Z",
            "location": "611 Historic Nature Trail Gatlinburg TN 37738 US",
        },
    ]


def test_plan_lodging_legs_drops_the_local_drive_the_outbound_absorbed(tmp_path, monkeypatch):
    """The hotel→venue drive the outbound went straight past must not survive
    into the reconcile input: planning both halves would put two drives on the
    calendar for one journey."""
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    hotel = "611 Historic Nature Trail Gatlinburg TN 37738 US"
    home = "12 Example St, Sampleton, TN 37000"
    stadium = "Stadium"
    local = DesiredBlock(
        identity="mtg-1",
        kind="meeting_outbound",
        summary="Drive: Opening Ceremony",
        start=datetime(2020, 8, 14, 23, 0, tzinfo=UTC),
        end=datetime(2020, 8, 15, 0, 0, tzinfo=UTC),
        origin=hotel,
        destination=stadium,
        baseline_seconds=3600,
        anchor=datetime(2020, 8, 15, 0, 0, tzinfo=UTC),
        timezone="America/New_York",
    )
    unrelated = DesiredBlock(
        identity="mtg-2",
        kind="meeting_outbound",
        summary="Drive: Dentist",
        start=datetime(2020, 8, 3, 14, 0, tzinfo=UTC),
        end=datetime(2020, 8, 3, 14, 30, tzinfo=UTC),
        origin=home,
        destination="Dentist",
        baseline_seconds=1800,
        anchor=datetime(2020, 8, 3, 14, 30, tzinfo=UTC),
    )

    def route(origin, destination):
        return timedelta(hours=2)

    blocks, skipped, questions, kept, _held = reconcile_sweep._plan_lodging_legs(
        schedule=_drive_trip_schedule(),
        home=home,
        route=route,
        now=datetime(2020, 8, 7, 12, 0, tzinfo=UTC),
        meeting_blocks=[local, unrelated],
    )

    assert [block.identity for block in kept] == ["mtg-2"]
    assert any(block.destination == stadium for block in blocks)
    assert questions == []
    assert any("absorbed" in note for note in skipped)


def test_plan_lodging_legs_keeps_every_meeting_block_when_no_trip_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    block = DesiredBlock(
        identity="mtg-9",
        kind="meeting_outbound",
        summary="Drive: Dentist",
        start=datetime(2020, 8, 3, 14, 0, tzinfo=UTC),
        end=datetime(2020, 8, 3, 14, 30, tzinfo=UTC),
        origin="home",
        destination="Dentist",
        baseline_seconds=1800,
        anchor=datetime(2020, 8, 3, 14, 30, tzinfo=UTC),
    )
    blocks, skipped, questions, kept, _held = reconcile_sweep._plan_lodging_legs(
        schedule=[],
        home="home",
        route=lambda origin, destination: timedelta(hours=1),
        now=datetime(2020, 8, 7, 12, 0, tzinfo=UTC),
        meeting_blocks=[block],
    )
    assert kept == [block]
    assert (blocks, skipped, questions) == ([], [], [])


def test_trip_presence_marks_the_first_and_last_event_of_each_trip():
    """Those two are where a home endpoint is real rather than a stale anchor."""

    class _M:
        def __init__(self, mid, start):
            self.meeting_id = mid
            self.start = start
            self.location = "Stadium"

    class _T:
        key = "t1"
        address = "611 Historic Nature Trail"
        check_out = None
        span_start = datetime(2020, 8, 14, tzinfo=UTC)
        span_end = datetime(2020, 8, 16, tzinfo=UTC)

    meetings = [
        _M("late", datetime(2020, 8, 15, 22, tzinfo=UTC)),
        _M("early", datetime(2020, 8, 14, 20, tzinfo=UTC)),
        _M("middle", datetime(2020, 8, 15, 2, tzinfo=UTC)),
        _M("offtrip", datetime(2020, 8, 30, 12, tzinfo=UTC)),
    ]
    presence = reconcile_sweep._trip_presence(
        [_T()], meetings, route=lambda o, d: timedelta(minutes=15)
    )

    assert set(presence) == {"early", "middle", "late"}
    assert (presence["early"].is_first, presence["early"].is_last) == (True, False)
    assert (presence["middle"].is_first, presence["middle"].is_last) == (False, False)
    assert (presence["late"].is_first, presence["late"].is_last) == (False, True)
    assert presence["early"].lodging == "611 Historic Nature Trail"


def test_trip_presence_is_empty_without_a_driving_trip():
    assert reconcile_sweep._trip_presence([], [], route=lambda o, d: timedelta(minutes=5)) == {}


class _PresenceTrip:
    """A DriveTrip stand-in carrying only what `_trip_presence` reads."""

    def __init__(self, key, address, start_day=14, end_day=16):
        self.key = key
        self.address = address
        self.check_out = None
        self.span_start = datetime(2020, 8, start_day, tzinfo=UTC)
        self.span_end = datetime(2020, 8, end_day, tzinfo=UTC)


class _PresenceMeeting:
    def __init__(self, mid, start, location="Stadium"):
        self.meeting_id = mid
        self.start = start
        self.location = location


def _distance_route(table):
    return lambda origin, destination: table.get(origin)


def test_overlapping_trips_give_the_meeting_to_the_nearer_lodging():
    """Both trips reach it; iteration order must not decide which claims it."""
    near = _PresenceTrip("near", "Near Hotel")
    far = _PresenceTrip("far", "Far Hotel")
    meeting = _PresenceMeeting("m", datetime(2020, 8, 15, 18, tzinfo=UTC))
    route = _distance_route(
        {"Near Hotel": timedelta(minutes=10), "Far Hotel": timedelta(minutes=90)}
    )

    for order in ([near, far], [far, near]):
        presence = reconcile_sweep._trip_presence(order, [meeting], route=route)
        assert presence["m"].lodging == "Near Hotel"
        assert (presence["m"].is_first, presence["m"].is_last) == (True, True)


def test_an_exact_tie_between_overlapping_trips_claims_nothing():
    """Declining is the safe direction: membership only ever grants the
    away-suppression exemption, so an unresolvable claim withholds it."""
    a = _PresenceTrip("a", "Hotel A")
    b = _PresenceTrip("b", "Hotel B")
    meeting = _PresenceMeeting("m", datetime(2020, 8, 15, 18, tzinfo=UTC))
    route = _distance_route({"Hotel A": timedelta(minutes=20), "Hotel B": timedelta(minutes=20)})

    assert reconcile_sweep._trip_presence([a, b], [meeting], route=route) == {}
    assert reconcile_sweep._trip_presence([b, a], [meeting], route=route) == {}


def test_overlapping_trips_keep_their_own_first_and_last_flags():
    """Each trip's flags are computed over the meetings it actually won, not
    over everything that fell on its dates."""
    near = _PresenceTrip("near", "Near Hotel")
    far = _PresenceTrip("far", "Far Hotel")
    shared = _PresenceMeeting("shared", datetime(2020, 8, 15, 18, tzinfo=UTC))
    near_only = _PresenceMeeting(
        "near-only", datetime(2020, 8, 14, 9, tzinfo=UTC), location="Near Venue"
    )
    far_only = _PresenceMeeting(
        "far-only", datetime(2020, 8, 16, 9, tzinfo=UTC), location="Far Venue"
    )

    def route(origin, destination):
        if destination == "Near Venue":
            return timedelta(minutes=5) if origin == "Near Hotel" else None
        if destination == "Far Venue":
            return timedelta(minutes=5) if origin == "Far Hotel" else None
        return timedelta(minutes=10) if origin == "Near Hotel" else timedelta(minutes=90)

    presence = reconcile_sweep._trip_presence(
        [near, far], [shared, near_only, far_only], route=route
    )
    # `near` won two meetings: the early one is first, the shared one is last.
    assert (presence["near-only"].is_first, presence["near-only"].is_last) == (True, False)
    assert (presence["shared"].is_first, presence["shared"].is_last) == (False, True)
    assert presence["shared"].lodging == "Near Hotel"
    # `far` won one, so it is both.
    assert (presence["far-only"].is_first, presence["far-only"].is_last) == (True, True)
    assert presence["far-only"].lodging == "Far Hotel"


def test_a_held_trip_keeps_the_local_drive_its_outbound_absorbed(tmp_path, monkeypatch):
    """#336 review: a sweep that absorbed hotel→venue into home→venue, then
    cannot route home→hotel, must not recreate hotel→venue beside the held
    outbound."""
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    hotel = "611 Historic Nature Trail Gatlinburg TN 37738 US"
    home = "12 Example St, Sampleton, TN 37000"
    local = DesiredBlock(
        identity="mtg-1",
        kind="meeting_outbound",
        summary="Drive: Opening Ceremony",
        start=datetime(2020, 8, 14, 23, 0, tzinfo=UTC),
        end=datetime(2020, 8, 15, 0, 0, tzinfo=UTC),
        origin=hotel,
        destination="Stadium",
        baseline_seconds=3600,
        anchor=datetime(2020, 8, 15, 0, 0, tzinfo=UTC),
    )

    def route(origin, destination):
        return None if origin == home else timedelta(minutes=20)

    _blocks, _skipped, _questions, kept, held = reconcile_sweep._plan_lodging_legs(
        schedule=_drive_trip_schedule(),
        home=home,
        route=route,
        now=datetime(2020, 8, 7, 12, 0, tzinfo=UTC),
        meeting_blocks=[local],
        verdicts={},
    )
    assert kept == []
    assert ("mtg-1", "meeting_outbound") in {leg.key for leg in held}


def test_resolve_a_refresh_without_coordinates_keeps_the_cached_pair():
    """#336 review: a near-term delay refresh missing lat/lon must not downgrade
    a cached routing point to routing by name."""
    static = StaticAirport(iata="BNA", flag="🇺🇸", timezone="America/Chicago", lat=36.12, lon=-86.68)
    fetched = _FakeCtx("BNA", flag="🇺🇸", delay_index="high", timezone="America/Chicago")
    resolved, new_static = _resolve_one_airport(
        static=static, want_delay=True, fetch=lambda: fetched
    )
    assert (resolved.lat, resolved.lon, resolved.delay_index) == (36.12, -86.68, "high")
    assert new_static is None  # nothing changed worth persisting


@pytest.mark.parametrize(
    "age_seconds,expected",
    [
        (-121, None),
        (-120, "36.1,-86.7"),
        (-11, "36.1,-86.7"),
        (0, "36.1,-86.7"),
        (1800, "36.1,-86.7"),
        (1801, None),
    ],
)
def test_live_fix_freshness_includes_mid_sweep_arrival(monkeypatch, age_seconds, expected):
    import state

    fix = {
        "captured_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "latitude": 36.1,
        "longitude": -86.7,
    }
    monkeypatch.setattr(state, "read_current_location", lambda: fix)
    assert reconcile_sweep._fresh_live_origin(NOW, 30) == expected


@pytest.mark.parametrize("live_origin,source", [("36.1,-86.7", "live_gps"), (None, "planned")])
def test_departure_origin_source_is_reported_without_location_values(live_origin, source):
    result = build_plan(
        flight_records=[_record(1, 4, 3, "2020-07-10T14:00:00Z", "2020-07-10T16:00:00Z")],
        resolve_airport=_resolve_airport,
        meeting_blocks=[],
        current_blocks=[],
        route=_route,
        now=NOW,
        home_address=HOME,
        live_origin=live_origin,
    )
    departures = [row for row in result.origin_sources if row["kind"] == "airport_departure"]
    assert len(departures) == 1
    assert departures[0]["source"] == source
    assert HOME not in json.dumps(departures)
    assert "36.1" not in json.dumps(departures)
