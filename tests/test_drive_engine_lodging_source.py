"""Tests for the lodging-leg source (flight-less drive trips → getting-there legs).

Deterministic fixtures only: a hand-built schedule in the shape
`travel-schedule.json` really has (a date-only `Trip` wrapper plus `Check-in:` /
`Check-out:` `Lodging` records), a fake router returning fixed durations, and an
injected `now`. Nothing reads a clock.

What is pinned here is the decision the module exists to make — which flight-less
trips get a drive planned — across all three drive-time bands, plus the two
anchoring rules that keep the outer legs from colliding with the local ones.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from drive_decision import (  # noqa: E402
    DECIDED_BY_DRIVE_TIME,
    DECIDED_BY_OPERATOR,
    VERDICT_DRIVE,
    VERDICT_FLY,
    VERDICT_UNKNOWN,
    TripVerdict,
)
from lodging_source import (  # noqa: E402
    DRIVE_CERTAIN_MAX,
    DRIVE_IMPLAUSIBLE_MIN,
    KIND_OUTBOUND,
    KIND_RETURN,
    LOCAL_TO_LODGING_MAX,
    DriveTrip,
    TripContext,
    classify_drive,
    context_from_blocks,
    driving_trips,
    find_drive_trips,
    lodging_desired_blocks,
    meetings_on_trip,
)
from reconcile import DesiredBlock  # noqa: E402

UTC = UTC
HOME = "12 Example St, Sampleton, TN 37000"
HOTEL_ADDRESS = "611 Historic Nature Trail Gatlinburg TN 37738 US"
HOTEL = "Fairfield Inn & Suites"

# The sweep runs a week before the trip; every fixture instant is fixed.
NOW = datetime(2020, 8, 7, 12, 0, tzinfo=UTC)
CHECK_IN = datetime(2020, 8, 14, 20, 0, tzinfo=UTC)
CHECK_OUT = datetime(2020, 8, 15, 15, 0, tzinfo=UTC)
TRIP_KEY = "tn-tigers-2020-08"


def _trip_record(summary: str = "TN Tigers", start: str = "2020-08-14", end: str = "2020-08-16"):
    return {"type": "Trip", "summary": summary, "start": start, "end": end, "location": "TN"}


def _lodging(role: str, when: datetime, *, location: str | None = HOTEL_ADDRESS):
    record = {
        "type": "Lodging",
        "summary": f"{'Check-in:' if role == 'in' else 'Check-out:'} {HOTEL}",
        "start": when.isoformat().replace("+00:00", "Z"),
        "end": (when + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }
    if location is not None:
        record["location"] = location
    return record


def _schedule(*extra):
    return [
        _trip_record(),
        _lodging("in", CHECK_IN),
        _lodging("out", CHECK_OUT),
        *extra,
    ]


def _router(*, out: timedelta | None, back: timedelta | None = None):
    """A route fn keyed on direction, so the two legs can differ or fail apart."""

    def route(origin: str, destination: str):
        if origin == HOME:
            return out
        return back if back is not None else out

    return route


def _plan(
    schedule=None,
    *,
    drive: timedelta,
    verdicts=None,
    contexts=None,
    now=NOW,
    home: str | None = HOME,
):
    trips = find_drive_trips(
        schedule if schedule is not None else _schedule(), now=now, window=timedelta(days=30)
    )
    return lodging_desired_blocks(
        trips,
        route=_router(out=drive),
        home_address=home,
        verdicts=verdicts or {},
        contexts=contexts,
        now=now,
    )


# ---------------------------------------------------------------------------
# Trip discovery
# ---------------------------------------------------------------------------


def test_finds_the_flightless_lodging_trip():
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    assert [t.key for t in trips] == [TRIP_KEY]
    trip = trips[0]
    assert (trip.check_in, trip.check_out) == (CHECK_IN, CHECK_OUT)
    assert trip.address == HOTEL_ADDRESS
    assert trip.hotel == HOTEL


def test_a_timed_flight_in_the_span_disqualifies_the_trip():
    """A flown trip's ground legs come from the airport chain, not from here."""
    flight = {"type": "Flight", "summary": "DL 123", "start": "2020-08-14T10:00:00Z"}
    assert find_drive_trips(_schedule(flight), now=NOW, window=timedelta(days=30)) == []


def test_a_date_only_flight_does_not_disqualify_the_trip():
    """A bare `YYYY-MM-DD` flight cannot time a departure; suppressing the drive
    on it would strand a trip whose flight the feed never timed."""
    flight = {"type": "Flight", "summary": "DL 123", "start": "2020-08-14"}
    assert [
        t.key for t in find_drive_trips(_schedule(flight), now=NOW, window=timedelta(days=30))
    ] == [TRIP_KEY]


def test_lodging_without_a_usable_address_is_not_a_drive_trip():
    """Routing needs a real address; a blank location is unroutable, not a drive."""
    schedule = [_trip_record(), _lodging("in", CHECK_IN, location="  "), _lodging("out", CHECK_OUT)]
    assert find_drive_trips(schedule, now=NOW, window=timedelta(days=30)) == []


def test_a_finished_trip_is_dropped_but_one_under_way_is_kept():
    """Mid-trip the return leg is still ahead, so the trip stays in scope."""
    mid_trip = datetime(2020, 8, 15, 9, 0, tzinfo=UTC)
    after = datetime(2020, 8, 20, 9, 0, tzinfo=UTC)
    assert [t.key for t in find_drive_trips(_schedule(), now=mid_trip, window=timedelta(days=30))]
    assert find_drive_trips(_schedule(), now=after, window=timedelta(days=30)) == []


def test_a_trip_beyond_the_window_is_out_of_scope():
    assert find_drive_trips(_schedule(), now=NOW, window=timedelta(days=2)) == []


def test_earliest_checkin_and_latest_checkout_bound_a_two_hotel_trip():
    """Two stays still yield ONE outer pair; the hop between them is a local drive."""
    second_in = datetime(2020, 8, 15, 18, 0, tzinfo=UTC)
    second_out = datetime(2020, 8, 16, 15, 0, tzinfo=UTC)
    schedule = _schedule(_lodging("in", second_in), _lodging("out", second_out))
    trip = find_drive_trips(schedule, now=NOW, window=timedelta(days=30))[0]
    assert (trip.check_in, trip.check_out) == (CHECK_IN, second_out)


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("drive", "expected"),
    [
        (timedelta(minutes=45), VERDICT_DRIVE),
        (DRIVE_CERTAIN_MAX, VERDICT_DRIVE),
        (DRIVE_CERTAIN_MAX + timedelta(minutes=1), VERDICT_UNKNOWN),
        (timedelta(hours=3, minutes=40), VERDICT_UNKNOWN),
        (DRIVE_IMPLAUSIBLE_MIN - timedelta(minutes=1), VERDICT_UNKNOWN),
        (DRIVE_IMPLAUSIBLE_MIN, VERDICT_FLY),
        (timedelta(hours=12), VERDICT_FLY),
    ],
)
def test_classify_drive_bands(drive, expected):
    assert classify_drive(drive) == expected


# ---------------------------------------------------------------------------
# Planning per band
# ---------------------------------------------------------------------------


def test_a_short_drive_builds_both_legs_and_asks_nothing():
    blocks, _skipped, plans, _held = _plan(drive=timedelta(hours=2))
    assert [b.kind for b in blocks] == [KIND_OUTBOUND, KIND_RETURN]
    assert plans[0].ask is None
    assert plans[0].effective == VERDICT_DRIVE

    outbound, returning = blocks
    assert (outbound.origin, outbound.destination) == (HOME, HOTEL_ADDRESS)
    assert (returning.origin, returning.destination) == (HOTEL_ADDRESS, HOME)


def test_a_very_long_drive_builds_nothing_and_asks_nothing():
    """Above the band it is a flight; the missing-flight gap is the booking
    check's to report, so this side stays silent."""
    blocks, _skipped, plans, _held = _plan(drive=timedelta(hours=9))
    assert blocks == []
    assert (plans[0].effective, plans[0].ask) == (VERDICT_FLY, None)


def test_an_ambiguous_drive_asks_once_and_builds_nothing_meanwhile():
    blocks, _skipped, plans, _held = _plan(drive=timedelta(hours=3, minutes=40))
    assert blocks == []
    assert plans[0].effective == VERDICT_UNKNOWN
    assert plans[0].ask is not None
    assert "3h40m" in plans[0].ask
    assert HOTEL in plans[0].ask


def test_an_already_asked_trip_is_not_asked_again():
    """Re-asking every sweep is the nag the verdict store exists to prevent."""
    asked = TripVerdict(
        verdict=VERDICT_UNKNOWN,
        decided_by=DECIDED_BY_DRIVE_TIME,
        drive_seconds=13200,
        asked_at=NOW - timedelta(hours=1),
        expires=CHECK_OUT + timedelta(days=2),
    )
    _blocks, _skipped, plans, _held = _plan(
        drive=timedelta(hours=3, minutes=40), verdicts={TRIP_KEY: asked}
    )
    assert plans[0].ask is None


@pytest.mark.parametrize(
    ("answer", "expect_blocks"),
    [(VERDICT_DRIVE, True), (VERDICT_FLY, False)],
)
def test_an_operator_answer_settles_the_ambiguous_band(answer, expect_blocks):
    verdict = TripVerdict(
        verdict=answer,
        decided_by=DECIDED_BY_OPERATOR,
        drive_seconds=13200,
        asked_at=NOW - timedelta(hours=1),
        expires=CHECK_OUT + timedelta(days=2),
    )
    blocks, _skipped, plans, _held = _plan(
        drive=timedelta(hours=3, minutes=40), verdicts={TRIP_KEY: verdict}
    )
    assert bool(blocks) is expect_blocks
    assert plans[0].effective == answer
    assert plans[0].ask is None


def test_an_operator_answer_outranks_the_drive_time_band():
    """A 9h drive the operator says they are driving is planned anyway — they
    know something the router does not."""
    verdict = TripVerdict(
        verdict=VERDICT_DRIVE,
        decided_by=DECIDED_BY_OPERATOR,
        drive_seconds=32400,
        asked_at=None,
        expires=CHECK_OUT + timedelta(days=2),
    )
    blocks, _skipped, plans, _held = _plan(drive=timedelta(hours=9), verdicts={TRIP_KEY: verdict})
    assert [b.kind for b in blocks] == [KIND_OUTBOUND, KIND_RETURN]
    assert plans[0].verdict == VERDICT_FLY  # what the band alone said
    assert plans[0].effective == VERDICT_DRIVE  # what the operator said


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def test_outbound_lands_by_checkin_when_no_local_drive_exists():
    blocks, _skipped, _plans, _held = _plan(drive=timedelta(hours=2))
    outbound = blocks[0]
    assert outbound.end == CHECK_IN
    assert outbound.start == CHECK_IN - timedelta(hours=2)


def test_outbound_lands_by_the_onward_drive_not_the_nominal_checkin():
    """Check-in is a nominal stamp; the onward drive is a real commitment, and
    arriving after it leaves has the operator miss the event."""
    onward = CHECK_IN - timedelta(hours=3)
    ctx = {
        TRIP_KEY: TripContext(
            onward_start=onward, trailing_end=CHECK_IN, timezone="America/New_York"
        )
    }
    blocks, _skipped, _plans, _held = _plan(drive=timedelta(hours=2), contexts=ctx)
    outbound = blocks[0]
    assert outbound.end == onward
    assert outbound.timezone == "America/New_York"


def test_return_departs_after_a_local_drive_that_outlasts_checkout():
    """An event after check-out moves the drive home; leaving at check-out would
    plan it straight across the event."""
    trailing = CHECK_OUT + timedelta(hours=4)
    ctx = {TRIP_KEY: TripContext(trailing_end=trailing)}
    blocks, _skipped, _plans, _held = _plan(drive=timedelta(hours=2), contexts=ctx)
    returning = blocks[1]
    assert returning.start == trailing
    assert returning.end == trailing + timedelta(hours=2)


def test_return_departs_at_checkout_when_nothing_trails_it():
    blocks, _skipped, _plans, _held = _plan(drive=timedelta(hours=2))
    assert blocks[1].start == CHECK_OUT


def test_context_from_blocks_reads_the_local_drives():
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    first = DesiredBlock(
        identity="mtg1",
        kind="meeting_outbound",
        summary="Drive: Game",
        start=CHECK_IN + timedelta(hours=2),
        end=CHECK_IN + timedelta(hours=3),
        origin=HOTEL_ADDRESS,
        destination="Stadium",
        baseline_seconds=3600,
        anchor=CHECK_IN + timedelta(hours=3),
        timezone="America/New_York",
    )
    later = DesiredBlock(
        identity="mtg2",
        kind="meeting_return",
        summary="Drive: Game",
        start=CHECK_IN + timedelta(hours=6),
        end=CHECK_IN + timedelta(hours=7),
        origin="Stadium",
        destination=HOTEL_ADDRESS,
        baseline_seconds=3600,
        anchor=CHECK_IN + timedelta(hours=6),
        timezone="America/New_York",
    )
    ctx = context_from_blocks(trips, [first, later])[TRIP_KEY]
    assert ctx.onward_start == first.start
    assert ctx.trailing_end == later.end
    assert ctx.timezone == "America/New_York"


def test_context_counts_a_local_drive_after_check_out():
    """The check-out-day evening game is the ordinary shape of a weekend trip.

    Clamping the window at check-out hid it, so the return leg departed at
    check-out and landed home before a game the calendar still had the operator
    driving to from a hotel they had left. Paired with `_plan` below so the
    context the real producer yields is the one the return leg is planned from —
    the hand-built `TripContext` in the sibling test cannot catch this.
    """
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    evening_out = DesiredBlock(
        identity="mtg3",
        kind="meeting_outbound",
        summary="Drive: Game",
        start=CHECK_OUT + timedelta(hours=6),
        end=CHECK_OUT + timedelta(hours=7),
        origin=HOTEL_ADDRESS,
        destination="Stadium",
        baseline_seconds=3600,
        anchor=CHECK_OUT + timedelta(hours=7),
        timezone="America/New_York",
    )
    evening_back = DesiredBlock(
        identity="mtg4",
        kind="meeting_return",
        summary="Drive: Game",
        start=CHECK_OUT + timedelta(hours=11),
        end=CHECK_OUT + timedelta(hours=12),
        origin="Stadium",
        destination=HOTEL_ADDRESS,
        baseline_seconds=3600,
        anchor=CHECK_OUT + timedelta(hours=11),
        timezone="America/New_York",
    )
    ctx = context_from_blocks(trips, [evening_out, evening_back])[TRIP_KEY]
    assert ctx.trailing_end == evening_back.end

    blocks, _skipped, _plans, _held = _plan(drive=timedelta(hours=2), contexts={TRIP_KEY: ctx})
    # Departs the STADIUM as that last drive would have set off, not the hotel
    # when it would have arrived: the room is already released, so the drive
    # back to it is absorbed. What this test pins either way is that the return
    # is not planned at check-out, hours before the event.
    assert blocks[1].start == evening_back.start
    assert blocks[1].start > CHECK_OUT


def _local(name, kind, start, end, origin, destination, tz="America/New_York"):
    return DesiredBlock(
        identity=name,
        kind=kind,
        summary="Drive: Opening Ceremony" if "out" in name else "Drive: Game",
        start=start,
        end=end,
        origin=origin,
        destination=destination,
        baseline_seconds=int((end - start).total_seconds()),
        anchor=start if kind.endswith("return") else end,
        timezone=tz,
    )


def _venue_router(*, home_hotel, home_venue, hotel_home, venue_home):
    """Routes keyed on the pair, so a direct leg and a via-the-hotel leg differ."""

    def route(origin, destination):
        return {
            (HOME, HOTEL_ADDRESS): home_hotel,
            (HOME, "Stadium"): home_venue,
            (HOTEL_ADDRESS, HOME): hotel_home,
            ("Stadium", HOME): venue_home,
        }.get((origin, destination))

    return route


def _plan_with_venue(local_blocks, **route_kw):
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    ctx = context_from_blocks(trips, local_blocks)[TRIP_KEY]
    blocks, skipped, plans, _held = lodging_desired_blocks(
        trips,
        route=_venue_router(**route_kw),
        home_address=HOME,
        verdicts={},
        contexts={TRIP_KEY: ctx},
        now=NOW,
    )
    return blocks, skipped, plans, ctx


def test_outbound_drives_straight_to_the_venue_when_the_event_is_first():
    """Landing at the hotel exactly as the hotel→venue drive departs is a stop
    made only to leave it. Go to the venue and absorb that local drive."""
    out = _local(
        "mtg-out",
        "meeting_outbound",
        CHECK_IN + timedelta(hours=3),
        CHECK_IN + timedelta(hours=4),
        HOTEL_ADDRESS,
        "Stadium",
    )
    blocks, _skipped, plans, ctx = _plan_with_venue(
        [out],
        home_hotel=timedelta(hours=2),
        home_venue=timedelta(hours=2, minutes=30),
        hotel_home=timedelta(hours=2),
        venue_home=timedelta(hours=2, minutes=30),
    )
    assert ctx.first_out is not None and ctx.first_out.venue == "Stadium"
    outbound = blocks[0]
    assert outbound.destination == "Stadium"
    assert outbound.end == out.end
    assert outbound.start == out.end - timedelta(hours=2, minutes=30)
    assert outbound.summary == "Drive: home → Opening Ceremony"
    assert plans[0].subsumed == (("mtg-out", "meeting_outbound"),)


def test_outbound_keeps_the_hotel_when_no_local_drive_leaves_it():
    """Nothing to absorb — the trip's first commitment is the stay itself."""
    blocks, _skipped, plans, _ctx = _plan_with_venue(
        [],
        home_hotel=timedelta(hours=2),
        home_venue=timedelta(hours=3),
        hotel_home=timedelta(hours=2),
        venue_home=timedelta(hours=3),
    )
    assert blocks[0].destination == HOTEL_ADDRESS
    assert blocks[0].end == CHECK_IN
    assert plans[0].subsumed == ()


def test_outbound_falls_back_to_the_hotel_when_the_venue_route_fails():
    """A failed direct route degrades to the old shape rather than dropping the
    leg, and the local drive it would have absorbed stays planned."""
    out = _local(
        "mtg-out",
        "meeting_outbound",
        CHECK_IN + timedelta(hours=3),
        CHECK_IN + timedelta(hours=4),
        HOTEL_ADDRESS,
        "Stadium",
    )
    blocks, skipped, plans, _ctx = _plan_with_venue(
        [out],
        home_hotel=timedelta(hours=2),
        home_venue=None,
        hotel_home=timedelta(hours=2),
        venue_home=timedelta(hours=2),
    )
    assert blocks[0].destination == HOTEL_ADDRESS
    assert plans[0].subsumed == ()
    assert any("route failed" in note for note in skipped)


def test_return_departs_the_venue_when_the_room_is_already_released():
    back = _local(
        "mtg-back",
        "meeting_return",
        CHECK_OUT + timedelta(hours=8),
        CHECK_OUT + timedelta(hours=9),
        "Stadium",
        HOTEL_ADDRESS,
    )
    blocks, _skipped, plans, ctx = _plan_with_venue(
        [back],
        home_hotel=timedelta(hours=2),
        home_venue=timedelta(hours=2),
        hotel_home=timedelta(hours=2),
        venue_home=timedelta(hours=2, minutes=30),
    )
    assert ctx.last_back is not None and ctx.last_back.venue == "Stadium"
    returning = blocks[-1]
    assert returning.origin == "Stadium"
    assert returning.start == back.start
    assert returning.end == back.start + timedelta(hours=2, minutes=30)
    assert ("mtg-back", "meeting_return") in plans[0].subsumed


def test_return_still_goes_via_the_hotel_when_check_out_is_later():
    """The room is still held, so the drive back to it is a real leg — collect
    the bags, then leave."""
    back = _local(
        "mtg-back",
        "meeting_return",
        CHECK_OUT - timedelta(hours=6),
        CHECK_OUT - timedelta(hours=5),
        "Stadium",
        HOTEL_ADDRESS,
    )
    blocks, _skipped, plans, _ctx = _plan_with_venue(
        [back],
        home_hotel=timedelta(hours=2),
        home_venue=timedelta(hours=2),
        hotel_home=timedelta(hours=2),
        venue_home=timedelta(hours=2, minutes=30),
    )
    returning = blocks[-1]
    assert returning.origin == HOTEL_ADDRESS
    assert returning.start == CHECK_OUT
    assert plans[0].subsumed == ()


def test_context_ignores_blocks_anchored_after_the_trip_ends():
    """The trip wrapper's end still bounds the window — a drive the following
    week is not this trip's local traffic."""
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    next_week = DesiredBlock(
        identity="mtg8",
        kind="meeting_outbound",
        summary="Drive: Dentist",
        start=CHECK_OUT + timedelta(days=7),
        end=CHECK_OUT + timedelta(days=7, minutes=30),
        origin=HOME,
        destination="Dentist",
        baseline_seconds=1800,
        anchor=CHECK_OUT + timedelta(days=7, minutes=30),
    )
    assert context_from_blocks(trips, [next_week])[TRIP_KEY] == TripContext()


def test_context_ignores_blocks_anchored_outside_the_stay():
    """A drive at home the week before is not this trip's local traffic."""
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    unrelated = DesiredBlock(
        identity="mtg9",
        kind="meeting_outbound",
        summary="Drive: Dentist",
        start=NOW,
        end=NOW + timedelta(minutes=30),
        origin=HOME,
        destination="Dentist",
        baseline_seconds=1800,
        anchor=NOW + timedelta(minutes=30),
    )
    ctx = context_from_blocks(trips, [unrelated])[TRIP_KEY]
    assert ctx == TripContext()


# ---------------------------------------------------------------------------
# Degraded inputs
# ---------------------------------------------------------------------------


def test_a_failed_outbound_route_skips_the_trip_with_a_diagnostic():
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    blocks, skipped, plans, _held = lodging_desired_blocks(
        trips,
        route=_router(out=None),
        home_address=HOME,
        verdicts={},
        now=NOW,
    )
    assert blocks == [] and plans == []
    assert any("route failed" in s for s in skipped)


def test_a_failed_return_route_keeps_the_outbound_leg():
    """Half a plan beats none — the drive there is still correct."""
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))

    def route(origin: str, _destination: str):
        return timedelta(hours=2) if origin == HOME else None

    blocks, skipped, _plans, _held = lodging_desired_blocks(
        trips, route=route, home_address=HOME, verdicts={}, now=NOW
    )
    assert [b.kind for b in blocks] == [KIND_OUTBOUND]
    assert any("lodging→home route failed" in s for s in skipped)


def test_no_home_address_plans_nothing_and_says_so():
    """Guessing an origin would mis-time every leg; refuse loudly instead."""
    blocks, skipped, plans, _held = _plan(drive=timedelta(hours=2), home=None)
    assert blocks == [] and plans == []
    assert any("no home address configured" in s for s in skipped)


def test_a_past_outbound_is_dropped_while_the_return_still_builds():
    """Mid-trip the drive there has already happened; the drive home has not."""
    mid_trip = CHECK_IN + timedelta(hours=6)
    blocks, skipped, _plans, _held = _plan(drive=timedelta(hours=2), now=mid_trip)
    assert [b.kind for b in blocks] == [KIND_RETURN]
    assert any("outbound: past" in s for s in skipped)


def test_both_legs_carry_the_trip_key_as_identity():
    """Reconcile keys on (identity, kind); sharing the trip key is what makes a
    re-plan update the same two blocks instead of stacking new ones."""
    blocks, _skipped, _plans, _held = _plan(drive=timedelta(hours=2))
    assert {b.identity for b in blocks} == {TRIP_KEY}
    assert len({b.kind for b in blocks}) == 2


def test_a_timed_rail_segment_disqualifies_the_trip_too():
    """A train to the destination is not a drive; planning one would double up
    on a journey already booked."""
    rail = {"type": "Rail", "summary": "Amtrak 20", "start": "2020-08-14T10:00:00Z"}
    assert find_drive_trips(_schedule(rail), now=NOW, window=timedelta(days=30)) == []


def test_a_car_rental_does_not_disqualify_the_trip():
    """Renting a car is compatible with driving there, not evidence against it."""
    rental = {"type": "Car Rental", "summary": "Hertz", "start": "2020-08-14T21:00:00Z"}
    assert [
        t.key for t in find_drive_trips(_schedule(rental), now=NOW, window=timedelta(days=30))
    ] == [TRIP_KEY]


# ---------------------------------------------------------------------------
# Orphan check-in — a stay TripIt wrote with no check-out record
# ---------------------------------------------------------------------------


def _orphan_schedule():
    return [_trip_record(), _lodging("in", CHECK_IN)]


def test_an_orphan_checkin_still_sees_its_local_drives():
    """Bounding the window at the check-in instant made every local drive
    invisible; the trip wrapper's end is the right outer bound."""
    trips = find_drive_trips(_orphan_schedule(), now=NOW, window=timedelta(days=30))
    assert trips[0].check_out is None

    local = DesiredBlock(
        identity="mtg1",
        kind="meeting_outbound",
        summary="Drive: Game",
        start=CHECK_IN + timedelta(hours=2),
        end=CHECK_IN + timedelta(hours=5),
        origin=HOTEL_ADDRESS,
        destination="Stadium",
        baseline_seconds=3600,
        anchor=CHECK_IN + timedelta(hours=4),
        timezone="America/New_York",
    )
    ctx = context_from_blocks(trips, [local])[TRIP_KEY]
    assert ctx.onward_start == local.start
    assert ctx.trailing_end == local.end


def test_an_orphan_checkin_still_gets_a_return_leg_from_its_trailing_drive():
    """With no check-out to depart after, the last local drive is what the
    drive home follows — dropping it stranded the operator at the hotel."""
    trailing_end = CHECK_IN + timedelta(hours=5)
    ctx = {TRIP_KEY: TripContext(trailing_end=trailing_end)}
    blocks, _skipped, _plans, _held = _plan(
        _orphan_schedule(), drive=timedelta(hours=2), contexts=ctx
    )
    assert [b.kind for b in blocks] == [KIND_OUTBOUND, KIND_RETURN]
    assert blocks[1].start == trailing_end


def test_an_orphan_checkin_with_no_local_drives_plans_no_return():
    """Nothing to anchor on: the trip wrapper's date-only midnight is a worse
    departure time than none, so the leg is skipped with a diagnostic."""
    blocks, skipped, _plans, _held = _plan(_orphan_schedule(), drive=timedelta(hours=2))
    assert [b.kind for b in blocks] == [KIND_OUTBOUND]
    assert any("no check-out to depart after" in s for s in skipped)


# ---------------------------------------------------------------------------
# The plan must not depend on where the check-in stamp sits (#242)
# ---------------------------------------------------------------------------


def _venue_leg(name, kind, start, end, origin, destination):
    return DesiredBlock(
        identity=name,
        kind=kind,
        summary="Drive: Ceremony" if name.startswith("cer") else "Drive: Game",
        start=start,
        end=end,
        origin=origin,
        destination=destination,
        baseline_seconds=int((end - start).total_seconds()),
        anchor=start if kind.endswith("return") else end,
        timezone="America/New_York",
    )


def _two_event_blocks(first_out_origin):
    """The four local drives of a two-event trip.

    `first_out_origin` is where the first event's outbound starts — the hotel
    when check-in precedes it, home when the operator has not checked in yet.
    Everything else is identical, which is the point of the test.
    """
    return [
        _venue_leg(
            "cer-out",
            "meeting_outbound",
            CHECK_IN + timedelta(hours=3),
            CHECK_IN + timedelta(hours=4),
            first_out_origin,
            "Stadium",
        ),
        _venue_leg(
            "cer-back",
            "meeting_return",
            CHECK_IN + timedelta(hours=6),
            CHECK_IN + timedelta(hours=7),
            "Stadium",
            HOTEL_ADDRESS,
        ),
        _venue_leg(
            "game-out",
            "meeting_outbound",
            CHECK_OUT + timedelta(hours=6),
            CHECK_OUT + timedelta(hours=7),
            HOTEL_ADDRESS,
            "Stadium",
        ),
        _venue_leg(
            "game-back",
            "meeting_return",
            CHECK_OUT + timedelta(hours=11),
            CHECK_OUT + timedelta(hours=12),
            "Stadium",
            HOTEL_ADDRESS,
        ),
    ]


def _outer_legs(check_in, blocks):
    schedule = [
        _trip_record(),
        _lodging("in", check_in),
        _lodging("out", CHECK_OUT),
    ]
    trips = find_drive_trips(schedule, now=NOW, window=timedelta(days=30))
    ctx = context_from_blocks(trips, blocks)[TRIP_KEY]
    out, _skipped, plans, _held = lodging_desired_blocks(
        trips,
        route=_venue_router(
            home_hotel=timedelta(hours=2),
            home_venue=timedelta(hours=3),
            hotel_home=timedelta(hours=2),
            venue_home=timedelta(hours=3),
        ),
        home_address=HOME,
        verdicts={},
        contexts={TRIP_KEY: ctx},
        now=NOW,
    )
    return [(b.kind, b.start, b.end, b.origin, b.destination) for b in out], plans[0].subsumed


def test_outer_legs_are_identical_whether_check_in_precedes_the_first_event():
    """A check-in stamp is a fact about a reservation, not an instruction about
    which drives count. Stamping it after the first event used to hide that
    event's drives and re-anchor the outbound on the next day's."""
    before, subsumed_before = _outer_legs(CHECK_IN, _two_event_blocks(HOTEL_ADDRESS))
    after, subsumed_after = _outer_legs(CHECK_IN + timedelta(hours=6), _two_event_blocks(HOME))
    assert before == after
    assert subsumed_before == subsumed_after
    # And it is the real plan, not two matching empties.
    assert [kind for kind, *_ in before] == [KIND_OUTBOUND, KIND_RETURN]
    assert before[0][4] == "Stadium"


def test_a_home_errand_on_the_trips_first_morning_is_not_the_trips_first_leg():
    """Widening the window to the trip's own span let non-trip drives in. Only
    drives touching a venue the trip's lodging-anchored drives reach count."""
    errand = _venue_leg(
        "errand",
        "meeting_outbound",
        CHECK_IN - timedelta(hours=5),
        CHECK_IN - timedelta(hours=4),
        HOME,
        "Dentist",
    )
    legs, _subsumed = _outer_legs(CHECK_IN, [*_two_event_blocks(HOTEL_ADDRESS), errand])
    baseline, _ = _outer_legs(CHECK_IN, _two_event_blocks(HOTEL_ADDRESS))
    assert legs == baseline


def test_driving_trips_reports_only_the_trips_with_a_drive_verdict():
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    short = _router(out=timedelta(hours=1))
    assert [t.key for t in driving_trips(trips, route=short, home_address=HOME, verdicts={})] == [
        TRIP_KEY
    ]
    long_haul = _router(out=DRIVE_IMPLAUSIBLE_MIN + timedelta(hours=1))
    assert driving_trips(trips, route=long_haul, home_address=HOME, verdicts={}) == []
    ambiguous = _router(out=DRIVE_CERTAIN_MAX + timedelta(minutes=30))
    assert driving_trips(trips, route=ambiguous, home_address=HOME, verdicts={}) == []
    answered = {
        TRIP_KEY: TripVerdict(
            verdict=VERDICT_DRIVE,
            decided_by=DECIDED_BY_OPERATOR,
            drive_seconds=1,
            asked_at=None,
            expires=CHECK_OUT + timedelta(days=1),
        )
    }
    assert [
        t.key for t in driving_trips(trips, route=ambiguous, home_address=HOME, verdicts=answered)
    ] == [TRIP_KEY]


def test_driving_trips_does_not_claim_a_trip_whose_route_failed():
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    assert driving_trips(trips, route=lambda o, d: None, home_address=HOME, verdicts={}) == []
    assert (
        driving_trips(trips, route=_router(out=timedelta(hours=1)), home_address=None, verdicts={})
        == []
    )


@dataclass(frozen=True)
class FakeMeeting:
    """Duck-typed to the `scan.MeetingClass` surface `meetings_on_trip` reads."""

    meeting_id: str
    start: datetime | None
    location: str | None = "Stadium"


def _meeting(mid, start, location: str | None = "Stadium"):
    return FakeMeeting(meeting_id=mid, start=start, location=location)


def test_meetings_on_trip_takes_the_last_evening_but_not_the_next_week():
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    local = _router(out=timedelta(minutes=20))
    # The wrapper ends 2020-08-16 00:00; an event that evening is still on it.
    late = _meeting("late", datetime(2020, 8, 16, 23, 0, tzinfo=UTC))
    early = _meeting("early", datetime(2020, 8, 14, 1, 0, tzinfo=UTC))
    off = _meeting("off", datetime(2020, 8, 20, 12, 0, tzinfo=UTC))
    undated = _meeting("undated", None)
    assert set(meetings_on_trip(trips[0], [late, early, off, undated], route=local)) == {
        "late",
        "early",
    }


def test_a_meeting_far_from_the_lodging_is_not_on_the_trip():
    """Dates alone would hand every appointment during the trip a TripPresence —
    exempting a home meeting from the implausible-drive suppression and inventing
    a cross-country block, the failure that suppression exists to prevent."""
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    during = _meeting("home-appt", CHECK_IN + timedelta(hours=20), location="Dentist")

    far = _router(out=LOCAL_TO_LODGING_MAX + timedelta(minutes=1))
    assert meetings_on_trip(trips[0], [during], route=far) == {}

    near = _router(out=LOCAL_TO_LODGING_MAX)
    assert meetings_on_trip(trips[0], [during], route=near) == {"home-appt": LOCAL_TO_LODGING_MAX}


def test_an_unroutable_or_placeless_meeting_is_not_on_the_trip():
    """Membership only ever GRANTS an exemption, so the unknown case declines."""
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    placeless = _meeting("no-loc", CHECK_IN + timedelta(hours=20), location=None)
    normal = _meeting("m", CHECK_IN + timedelta(hours=20))
    assert meetings_on_trip(trips[0], [placeless], route=_router(out=timedelta(minutes=5))) == {}
    assert meetings_on_trip(trips[0], [normal], route=lambda o, d: None) == {}


def test_meetings_on_trip_upper_bound_matches_the_context_window():
    """A stay recorded past the wrapper's end extends the bound, the same
    `max(span_end, check_out)` reading `context_from_blocks` takes — otherwise a
    meeting the context counts as local misses the exemption it grants.

    Built directly rather than through `find_drive_trips`, which cannot produce
    this shape: it only pairs lodging records inside the wrapper's own span."""
    late_out = CHECK_OUT + timedelta(days=3)
    trip = DriveTrip(
        key=TRIP_KEY,
        summary="TN Tigers",
        hotel=HOTEL,
        address=HOTEL_ADDRESS,
        check_in=CHECK_IN,
        check_out=late_out,
        span_start=datetime(2020, 8, 14, tzinfo=UTC),
        span_end=datetime(2020, 8, 16, tzinfo=UTC),
        expires=late_out + timedelta(days=2),
    )
    local = _router(out=timedelta(minutes=5))
    inside_stay = _meeting("after", late_out - timedelta(hours=2))
    assert set(meetings_on_trip(trip, [inside_stay], route=local)) == {"after"}

    beyond = _meeting("beyond", late_out + timedelta(days=2))
    assert meetings_on_trip(trip, [beyond], route=local) == {}


def _held_with_venue(local_blocks, **route_kw):
    trips = find_drive_trips(_schedule(), now=NOW, window=timedelta(days=30))
    ctx = context_from_blocks(trips, local_blocks)[TRIP_KEY]
    return lodging_desired_blocks(
        trips,
        route=_venue_router(**route_kw),
        home_address=HOME,
        verdicts={},
        contexts={TRIP_KEY: ctx},
        now=NOW,
    )


def test_a_return_that_cannot_route_holds_the_local_drive_it_absorbed():
    """#336 review: an earlier sweep left from the stadium and absorbed the drive
    back to the released room. With neither way home routable, that drive is
    held under the return, not recreated beside the retained return block."""
    back = _local(
        "mtg-back",
        "meeting_return",
        CHECK_OUT + timedelta(hours=8),
        CHECK_OUT + timedelta(hours=9),
        "Stadium",
        HOTEL_ADDRESS,
    )
    _blocks, _skipped, _plans, held = _held_with_venue(
        [back],
        home_hotel=timedelta(hours=2),
        home_venue=timedelta(hours=2),
        hotel_home=None,
        venue_home=None,
    )
    by_key = {leg.key: leg for leg in held}
    assert set(by_key) == {(TRIP_KEY, KIND_RETURN), ("mtg-back", "meeting_return")}
    assert by_key[("mtg-back", "meeting_return")].announce is False
    # #336 review: the return is due when it would leave, from the stadium
    # after the evening game, not at the noon check-out.
    assert by_key[(TRIP_KEY, KIND_RETURN)].when == back.start


def test_a_return_with_the_room_still_held_holds_only_itself():
    back = _local(
        "mtg-back",
        "meeting_return",
        CHECK_OUT - timedelta(hours=6),
        CHECK_OUT - timedelta(hours=5),
        "Stadium",
        HOTEL_ADDRESS,
    )
    _blocks, _skipped, _plans, held = _held_with_venue(
        [back],
        home_hotel=timedelta(hours=2),
        home_venue=timedelta(hours=2),
        hotel_home=None,
        venue_home=None,
    )
    assert [leg.key for leg in held] == [(TRIP_KEY, KIND_RETURN)]


def test_an_unplannable_trip_holds_its_local_drives_under_the_outer_legs():
    out = _local(
        "mtg-out",
        "meeting_outbound",
        CHECK_IN + timedelta(hours=3),
        CHECK_IN + timedelta(hours=4),
        HOTEL_ADDRESS,
        "Stadium",
    )
    _blocks, _skipped, _plans, held = _held_with_venue(
        [out], home_hotel=None, home_venue=None, hotel_home=None, venue_home=None
    )
    by_key = {leg.key: leg for leg in held}
    assert by_key[("mtg-out", "meeting_outbound")].announce is False
    assert by_key[(TRIP_KEY, KIND_OUTBOUND)].when == out.start


def test_an_unplannable_trip_returns_after_its_last_local_drive():
    """Check-out at noon, a game until the evening: the held return is due
    when the game ends, not at check-out."""
    game = _local(
        "mtg-game",
        "meeting_outbound",
        CHECK_OUT + timedelta(hours=6),
        CHECK_OUT + timedelta(hours=7),
        HOTEL_ADDRESS,
        "Stadium",
    )
    _blocks, _skipped, _plans, held = _held_with_venue(
        [game], home_hotel=None, home_venue=None, hotel_home=None, venue_home=None
    )
    returning = next(leg for leg in held if leg.key == (TRIP_KEY, KIND_RETURN))
    assert returning.when == game.end


def test_an_unplannable_trip_leaves_a_return_before_check_out_plannable():
    """#336 review: the home→hotel route failed, but a venue→hotel drive while
    the room is still held is no outer leg's to absorb; it stays desired."""
    back = _local(
        "mtg-back",
        "meeting_return",
        CHECK_OUT - timedelta(hours=6),
        CHECK_OUT - timedelta(hours=5),
        "Stadium",
        HOTEL_ADDRESS,
    )
    _blocks, _skipped, _plans, held = _held_with_venue(
        [back], home_hotel=None, home_venue=None, hotel_home=None, venue_home=None
    )
    assert ("mtg-back", "meeting_return") not in {leg.key for leg in held}
