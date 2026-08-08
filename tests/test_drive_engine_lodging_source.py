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
from datetime import datetime, timedelta, timezone
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
    TripContext,
    classify_drive,
    context_from_blocks,
    find_drive_trips,
    lodging_desired_blocks,
)
from reconcile import DesiredBlock  # noqa: E402

UTC = timezone.utc
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
    blocks, _skipped, plans = _plan(drive=timedelta(hours=2))
    assert [b.kind for b in blocks] == [KIND_OUTBOUND, KIND_RETURN]
    assert plans[0].ask is None
    assert plans[0].effective == VERDICT_DRIVE

    outbound, returning = blocks
    assert (outbound.origin, outbound.destination) == (HOME, HOTEL_ADDRESS)
    assert (returning.origin, returning.destination) == (HOTEL_ADDRESS, HOME)


def test_a_very_long_drive_builds_nothing_and_asks_nothing():
    """Above the band it is a flight; the missing-flight gap is the booking
    check's to report, so this side stays silent."""
    blocks, _skipped, plans = _plan(drive=timedelta(hours=9))
    assert blocks == []
    assert (plans[0].effective, plans[0].ask) == (VERDICT_FLY, None)


def test_an_ambiguous_drive_asks_once_and_builds_nothing_meanwhile():
    blocks, _skipped, plans = _plan(drive=timedelta(hours=3, minutes=40))
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
    _blocks, _skipped, plans = _plan(
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
    blocks, _skipped, plans = _plan(
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
    blocks, _skipped, plans = _plan(drive=timedelta(hours=9), verdicts={TRIP_KEY: verdict})
    assert [b.kind for b in blocks] == [KIND_OUTBOUND, KIND_RETURN]
    assert plans[0].verdict == VERDICT_FLY  # what the band alone said
    assert plans[0].effective == VERDICT_DRIVE  # what the operator said


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------


def test_outbound_lands_by_checkin_when_no_local_drive_exists():
    blocks, _skipped, _plans = _plan(drive=timedelta(hours=2))
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
    blocks, _skipped, _plans = _plan(drive=timedelta(hours=2), contexts=ctx)
    outbound = blocks[0]
    assert outbound.end == onward
    assert outbound.timezone == "America/New_York"


def test_return_departs_after_a_local_drive_that_outlasts_checkout():
    """An event after check-out moves the drive home; leaving at check-out would
    plan it straight across the event."""
    trailing = CHECK_OUT + timedelta(hours=4)
    ctx = {TRIP_KEY: TripContext(trailing_end=trailing)}
    blocks, _skipped, _plans = _plan(drive=timedelta(hours=2), contexts=ctx)
    returning = blocks[1]
    assert returning.start == trailing
    assert returning.end == trailing + timedelta(hours=2)


def test_return_departs_at_checkout_when_nothing_trails_it():
    blocks, _skipped, _plans = _plan(drive=timedelta(hours=2))
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
    blocks, skipped, plans = lodging_desired_blocks(
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

    blocks, skipped, _plans = lodging_desired_blocks(
        trips, route=route, home_address=HOME, verdicts={}, now=NOW
    )
    assert [b.kind for b in blocks] == [KIND_OUTBOUND]
    assert any("lodging→home route failed" in s for s in skipped)


def test_no_home_address_plans_nothing_and_says_so():
    """Guessing an origin would mis-time every leg; refuse loudly instead."""
    blocks, skipped, plans = _plan(drive=timedelta(hours=2), home=None)
    assert blocks == [] and plans == []
    assert any("no home address configured" in s for s in skipped)


def test_a_past_outbound_is_dropped_while_the_return_still_builds():
    """Mid-trip the drive there has already happened; the drive home has not."""
    mid_trip = CHECK_IN + timedelta(hours=6)
    blocks, skipped, _plans = _plan(drive=timedelta(hours=2), now=mid_trip)
    assert [b.kind for b in blocks] == [KIND_RETURN]
    assert any("outbound: past" in s for s in skipped)


def test_both_legs_carry_the_trip_key_as_identity():
    """Reconcile keys on (identity, kind); sharing the trip key is what makes a
    re-plan update the same two blocks instead of stacking new ones."""
    blocks, _skipped, _plans = _plan(drive=timedelta(hours=2))
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
    blocks, _skipped, _plans = _plan(_orphan_schedule(), drive=timedelta(hours=2), contexts=ctx)
    assert [b.kind for b in blocks] == [KIND_OUTBOUND, KIND_RETURN]
    assert blocks[1].start == trailing_end


def test_an_orphan_checkin_with_no_local_drives_plans_no_return():
    """Nothing to anchor on: the trip wrapper's date-only midnight is a worse
    departure time than none, so the leg is skipped with a diagnostic."""
    blocks, skipped, _plans = _plan(_orphan_schedule(), drive=timedelta(hours=2))
    assert [b.kind for b in blocks] == [KIND_OUTBOUND]
    assert any("no check-out to depart after" in s for s in skipped)
