"""Fixed synthetic owner windows through the real state/position/reconcile owners."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for bundle in ("travel-core", "drive-engine"):
    sys.path.insert(0, str(ROOT / "skills" / bundle))

from attendance_source import attendance_desired_blocks  # noqa: E402
from block_codec import parse_block  # noqa: E402
from calendar_apply import apply_plan  # noqa: E402
from conference_attendance import (  # noqa: E402
    AttendanceError,
    decode_store,
    parse_attendance,
    record_attendance,
)
from engine import itinerary_for  # noqa: E402
from meeting_source import meeting_desired_blocks  # noqa: E402
from reconcile import plan_reconcile  # noqa: E402
from reconcile_sweep import build_sweep_payload, meeting_anchor  # noqa: E402
from scan import scan  # noqa: E402

NOW = datetime(2020, 7, 13, 7, tzinfo=UTC)
VENUE, HOTEL = "100 Venue Street, Example City", "200 Hotel Street, Example City"


def row(**changes):
    return {
        "schema_version": 2,
        "id": "grounded-occurrence",
        "starts_on": "2020-07-13",
        "ends_on": "2020-07-14",
        "time_zone": "UTC",
        "venue_locations": ["ExampleConf"],
        "venue_location": VENUE,
        "days": [day()],
        **changes,
    }


def day(**changes):
    return {
        "date": "2020-07-13",
        "onsite_from": "09:00",
        "onsite_to": "17:00",
        "source": "owner",
        "source_ref": None,
        **changes,
    }


def schedule(checkout="2020-07-15T11:00:00Z"):
    return [
        {
            "type": "Trip",
            "summary": "Synthetic trip",
            "location": "Example City",
            "start": "2020-07-12",
            "end": "2020-07-15",
        },
        {
            "type": "Lodging",
            "summary": "Check-in: Example Hotel",
            "location": HOTEL,
            "start": "2020-07-12T15:00:00Z",
        },
        {
            "type": "Lodging",
            "summary": "Check-out: Example Hotel",
            "location": HOTEL,
            "start": checkout,
        },
    ]


def model(value=None, checkout="2020-07-15T11:00:00Z"):
    attendance = parse_attendance(row() if value is None else value)
    itinerary = itinerary_for(
        [], schedule(checkout), home_address="Home", attendances=(attendance,)
    )
    return attendance, itinerary


def event(identity, location, start=12, end=13):
    return {
        "id": identity,
        "summary": "Physical appointment",
        "location": location,
        "start": {"dateTime": NOW.replace(hour=start).isoformat()},
        "end": {"dateTime": NOW.replace(hour=end).isoformat()},
    }


def test_window_changes_position_and_nontight_outside_appointment_origin():
    attendance, itinerary = model()
    assert (
        itinerary.position_at(NOW.replace(hour=12)).address,
        itinerary.position_at(NOW.replace(hour=12)).source,
    ) == (VENUE, "attendance")
    meetings = scan(
        [event("outside", "Outside Office")],
        now=NOW,
        home_address="Home",
        attendances=(attendance,),
        anchor_for=lambda at: meeting_anchor(itinerary, at),
    )
    blocks, _, held = meeting_desired_blocks(meetings, route=lambda _a, _b: timedelta(minutes=20))
    assert held == []
    assert [(b.origin, b.destination) for b in blocks] == [
        (VENUE, "Outside Office"),
        ("Outside Office", VENUE),
    ]


def test_checkout_day_late_airport_position_stays_venue_after_window():
    _, itinerary = model(checkout="2020-07-13T11:00:00Z")
    assert itinerary.position_at(NOW.replace(hour=17, minute=52)).address == VENUE
    assert itinerary.position_at(NOW.replace(hour=17, minute=52)).source == "attendance"
    assert itinerary.position_at(NOW.replace(day=14, hour=8)).address is None


def test_daily_pair_exact_anchors_and_no_checkout_return():
    attendance, itinerary = model()
    blocks, skipped, held = attendance_desired_blocks(
        (attendance,),
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        window=timedelta(days=2),
    )
    assert skipped == held == []
    assert [
        (b.kind, b.origin, b.destination, b.start.hour, b.start.minute, b.end.hour) for b in blocks
    ] == [
        ("attendance_outbound", HOTEL, VENUE, 8, 40, 9),
        ("attendance_return", VENUE, HOTEL, 17, 0, 17),
    ]
    attendance, itinerary = model(checkout="2020-07-13T11:00:00Z")
    blocks, _, held = attendance_desired_blocks(
        (attendance,),
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        window=timedelta(days=2),
    )
    assert held == []
    assert [b.kind for b in blocks] == ["attendance_outbound"]


def test_no_window_is_ordinary_but_explicit_legacy_keeps_all_day_match():
    attendance, itinerary = model(row(days=[]))
    meetings = scan(
        [event("native", "Booth - ExampleConf")],
        now=NOW,
        home_address=HOTEL,
        attendances=(attendance,),
        anchor_for=lambda at: meeting_anchor(itinerary, at),
    )
    assert meetings[0].attendance_id is None and len(meetings[0].legs) == 2
    assert itinerary.position_at(NOW.replace(hour=12)).address == HOTEL
    legacy = {
        k: v for k, v in row().items() if k not in {"schema_version", "venue_location", "days"}
    }
    old = parse_attendance(legacy)
    meetings = scan(
        [event("native", "Booth - ExampleConf")], now=NOW, home_address=HOTEL, attendances=(old,)
    )
    assert meetings[0].attendance_id == old.id and meetings[0].legs == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"onsite_from": "09:00:00"},
        {"onsite_to": "09:00"},
        {"onsite_to": "24:00"},
        {"date": "2020-07-15"},
        {"source": "inferred"},
        {"source": "agenda"},
        {"source": "owner", "source_ref": "not an owner answer"},
    ],
)
def test_invalid_or_uncited_windows_hold(changes):
    with pytest.raises(AttendanceError):
        parse_attendance(row(days=[day(**changes)]))


@pytest.mark.parametrize(
    "when", [("2020-03-08", "02:30", "04:00"), ("2020-11-01", "01:30", "03:00")]
)
def test_dst_gaps_and_folds_are_not_silently_resolved(when):
    date, start, end = when
    with pytest.raises(AttendanceError, match="boundary"):
        parse_attendance(
            row(
                starts_on=date,
                ends_on=date,
                time_zone="America/New_York",
                days=[day(date=date, onsite_from=start, onsite_to=end)],
            )
        )


def test_exact_window_boundaries_and_actual_agenda_reference_retained():
    attendance = parse_attendance(
        row(days=[day(source="agenda", source_ref="https://example.test/agenda")])
    )
    assert attendance.days[0].source_ref == "https://example.test/agenda"
    start, end, _ = attendance.days[0].bounds("UTC")
    assert attendance.covers("native", "ExampleConf", start, end)
    assert not attendance.covers("native", "ExampleConf", start - timedelta(seconds=1), end)
    assert not attendance.covers("native", "ExampleConf", start, end + timedelta(seconds=1))
    _, itinerary = model()
    assert itinerary.position_at(end - timedelta(seconds=1)).source == "attendance"
    assert itinerary.position_at(end).source == "lodging"


def test_owner_migration_explicitly_preserves_legacy_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path))
    legacy = {
        k: v for k, v in row().items() if k not in {"schema_version", "venue_location", "days"}
    }
    path = tmp_path / "conference-attendance.json"
    raw = json.dumps({"schema_version": 1, "attendances": [legacy]}).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    assert decode_store(raw)[0].schema_version == 1
    assert path.read_bytes() == raw
    record_attendance(row(id="new"))
    value = json.loads(path.read_bytes())
    assert value["schema_version"] == 2
    assert value["attendances"][0] == {**legacy, "schema_version": 1}
    assert decode_store(path.read_bytes())[1].schema_version == 2
    assert all("schema_version" in item for item in value["attendances"])


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":3,"attendances":[]}',
        b"bad",
        b'{"schema_version":2,"attendances":[{"id":"legacy-without-version"}]}',
    ],
)
def test_unknown_and_damaged_stores_preserve_bytes_and_inode(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path))
    path = tmp_path / "conference-attendance.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    before = path.stat()
    with pytest.raises(AttendanceError):
        record_attendance(row())
    assert path.read_bytes() == raw
    assert path.stat().st_ino == before.st_ino


def test_calendar_pair_and_outside_drives_repeat_without_duplicate_or_skip_notice():
    attendance, itinerary = model()

    def route(_a, _b):
        return timedelta(minutes=20)

    meetings = scan(
        [
            event("onsite", "Booth - ExampleConf", 10, 11),
            event("outside", "Outside Office", 13, 14),
        ],
        now=NOW,
        home_address="Home",
        attendances=(attendance,),
        anchor_for=lambda at: meeting_anchor(itinerary, at),
    )
    meeting_blocks, skipped, held = meeting_desired_blocks(meetings, route=route)
    commutes, _, commute_held = attendance_desired_blocks(
        (attendance,), itinerary=itinerary, route=route, now=NOW, window=timedelta(days=2)
    )
    assert commute_held == held == []
    desired = meeting_blocks + commutes
    assert len(desired) == 4
    plan = plan_reconcile(desired, [], now=NOW)

    class Calendar:
        def __init__(self):
            self.events = []

        def create_event(self, args):
            native = {**args, "id": f"created-{len(self.events)}"}
            self.events.append(native)
            return native

    calendar = Calendar()
    applied = apply_plan(plan, calendar=calendar, now=NOW, creation_clock=lambda: NOW)
    current = [parse_block(e) for e in calendar.events]
    assert all(b is not None for b in current)
    again = plan_reconcile(desired, [b for b in current if b is not None], now=NOW)
    assert again.creates == again.updates == again.deletes == ()
    payload = build_sweep_payload(applied, skipped)
    assert len(calendar.events) == 4
    assert payload["data"]["message"] is not None  # real outside appointment notice remains


@pytest.mark.parametrize("fault", ["route", "conflict", "base"])
def test_uncertain_commute_is_held_not_deleted(fault):
    attendance, itinerary = model(
        checkout="2020-07-13T08:00:00Z" if fault == "base" else "2020-07-15T11:00:00Z"
    )

    def route(_a, _b):
        return None if fault == "route" else timedelta(minutes=20)

    _, skipped, held = attendance_desired_blocks(
        (attendance,),
        itinerary=itinerary,
        route=route,
        now=NOW,
        window=timedelta(days=2),
        busy_windows=[(NOW.replace(hour=8), NOW.replace(hour=9))] if fault == "conflict" else [],
    )
    assert skipped and any(item.kind == "attendance_outbound" for item in held)


def test_intake_three_choices_use_existing_owner_answer_without_state(
    tmp_path, monkeypatch, capsys
):
    import record_attendance as cli

    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(cli.sys, "argv", ["record_attendance.py", "--choices"])
    assert cli.main() == 0
    response = json.loads(capsys.readouterr().out)
    buttons = response["buttons"][0]
    assert [b["label"] for b in buttons] == ["По расписанию", "Только мои слоты", "Свои часы"]
    assert all(
        b == {"label": b["label"], "action": "owner.answer.v1", "payload": {"label": b["label"]}}
        for b in buttons
    )
    assert not (tmp_path / "absent").exists()


def test_no_window_checkout_does_not_guess_hotel_venue_or_city_and_other_dates_unchanged():
    _, itinerary = model(row(days=[]), checkout="2020-07-13T11:00:00Z")
    position = itinerary.position_at(NOW.replace(hour=17, minute=52))
    assert position.address is None and position.source == "unresolved"
    assert itinerary.position_at(NOW.replace(day=15, hour=12)).address == HOTEL


def test_outside_appointment_return_reanchors_when_window_ends():
    attendance, itinerary = model()

    def anchor(at):
        return meeting_anchor(itinerary, at)

    meetings = scan(
        [event("outside", "Outside Office", 16, 18)],
        now=NOW,
        home_address="Home",
        attendances=(attendance,),
        anchor_for=anchor,
        return_anchor_for=anchor,
    )
    blocks, _, held = meeting_desired_blocks(meetings, route=lambda _a, _b: timedelta(minutes=20))
    assert held == []
    assert [(b.origin, b.destination) for b in blocks] == [
        (VENUE, "Outside Office"),
        ("Outside Office", HOTEL),
    ]


def test_missing_busy_coverage_is_not_empty_coverage():
    attendance, itinerary = model()
    blocks, _, held = attendance_desired_blocks(
        (attendance,),
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        window=timedelta(days=2),
        busy_windows=None,
    )
    assert blocks == [] and len(held) == 2


def test_completed_flight_moves_operator_out_of_earlier_presence_window():
    from attendance_source import occurrence_scopes, presence_windows
    from itinerary import Itinerary, Transport

    attendance = parse_attendance(row())
    transport = Transport(NOW.replace(hour=10), NOW.replace(hour=11), "LGA", "BNA")
    itinerary = Itinerary(
        schedule("2020-07-13T09:00:00Z"),
        home_address="Home",
        home_airports=frozenset({"BNA"}),
        transports=[transport],
        attendances=presence_windows((attendance,)),
        attendance_scopes=occurrence_scopes((attendance,)),
    )
    assert itinerary.position_at(NOW.replace(hour=12)).address == "Home"
    assert itinerary.position_at(NOW.replace(hour=12)).source == "home"


def test_commute_already_underway_holds_prior_outbound():
    attendance, itinerary = model()
    blocks, _, held = attendance_desired_blocks(
        (attendance,),
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW.replace(hour=8, minute=50),
        window=timedelta(days=2),
    )
    assert [b.kind for b in blocks] == ["attendance_return"]
    assert len(held) == 1 and held[0].kind == "attendance_outbound"


def test_overlapping_grounded_windows_refuse_instead_of_picking_an_occurrence():
    attendance = parse_attendance(row())
    other = parse_attendance(row(id="other", venue_location="Other canonical address"))
    with pytest.raises(ValueError, match="overlapping"):
        itinerary_for([], schedule(), home_address="Home", attendances=(attendance, other))


def test_new_row2_cli_roundtrip_preserves_day_citation(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path))
    request = {
        "schema_version": 2,
        "attendances": [
            row(days=[day(source="agenda", source_ref="https://example.test/actual-schedule")])
        ],
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "skills/drive-engine/record_attendance.py")],
        input=json.dumps(request).encode(),
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "recorded": True,
        "id": "grounded-occurrence",
    }
    stored = decode_store((tmp_path / "conference-attendance.json").read_bytes())
    assert stored[0].days[0].source_ref == "https://example.test/actual-schedule"


@pytest.mark.parametrize(
    "citation",
    ["agenda", "http://example.test/schedule", "https://user:secret@example.test/schedule"],
)
def test_agenda_requires_actual_reference_shape_not_only_enum(citation):
    with pytest.raises(AttendanceError, match="reference"):
        parse_attendance(row(days=[day(source="agenda", source_ref=citation)]))


def test_row2_tight_bridges_use_grounded_address_and_retain_native_labels():
    attendance, itinerary = model()
    events = [
        event("onsite", "Booth - ExampleConf", 10, 11),
        {
            **event("outside", "Outside Office", 11, 12),
            "start": {"dateTime": NOW.replace(hour=11, minute=10).isoformat()},
        },
    ]
    meetings = scan(
        events,
        now=NOW,
        home_address="Home",
        attendances=(attendance,),
        anchor_for=lambda at: meeting_anchor(itinerary, at),
    )
    assert meetings[0].location == "Booth - ExampleConf"
    blocks, _, held = meeting_desired_blocks(meetings, route=lambda _a, _b: timedelta(minutes=5))
    assert held == []
    outbound = next(b for b in blocks if b.identity == "outside" and b.kind == "meeting_outbound")
    assert outbound.origin == VENUE and outbound.destination == "Outside Office"


def test_unrelated_dates_keep_legacy_start_anchor_even_with_other_row2_present():
    attendance = parse_attendance(row())
    future = event("unrelated", "Outside Office")
    future["start"] = {"dateTime": "2020-07-15T12:00:00Z"}
    future["end"] = {"dateTime": "2020-07-15T13:00:00Z"}
    meetings = scan(
        [future],
        now=NOW,
        home_address="Home",
        attendances=(attendance,),
        anchor_for=lambda _at: ("Original start base", None),
        return_anchor_for=lambda _at: ("Different end base", None),
    )
    assert meetings[0].legs[-1].destination == "Original start base"


def test_existing_flight_guard_holds_conflicting_commute_without_orphan_delete():
    from calendar_apply import build_create_args
    from engine import build_reconcile_plan
    from flight_identity import TRIPIT, Flight

    attendance, itinerary = model()
    blocks, _, _ = attendance_desired_blocks(
        (attendance,),
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        window=timedelta(days=2),
    )
    outbound = next(b for b in blocks if b.kind == "attendance_outbound")
    retained = parse_block(
        {"id": "owned-commute", **build_create_args(outbound, calendar_id="primary")}
    )
    assert retained is not None
    flight = Flight(
        "LGA",
        "BNA",
        NOW.replace(hour=10),
        NOW.replace(hour=12),
        source=TRIPIT,
        tripit_segment_id="synthetic-native-segment",
    )
    result = build_reconcile_plan(
        flights=[flight],
        airport_info={},
        current_blocks=[retained],
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        home_address="Home",
        extra_desired=[outbound],
    )
    assert any(h.kind == "attendance_outbound" for h in result.held)
    assert not any(c.desired.kind == "attendance_outbound" for c in result.plan.creates)
    assert not result.plan.deletes


@pytest.mark.parametrize("crosses_window", [True, False])
def test_transport_presence_fences_return_in_full_reconcile(crosses_window):
    from calendar_apply import build_create_args
    from engine import build_reconcile_plan
    from flight_identity import TRIPIT, Flight, merge_flights

    attendance, prior_itinerary = model()
    prior, _, _ = attendance_desired_blocks(
        (attendance,),
        itinerary=prior_itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        window=timedelta(days=2),
    )
    old_return = next(b for b in prior if b.kind == "attendance_return")
    retained = parse_block(
        {"id": "retained-return", **build_create_args(old_return, calendar_id="primary")}
    )
    assert retained is not None
    dep_hour, arr_hour = (10, 11) if crosses_window else (20, 21)
    flight = Flight(
        "LGA",
        "PHL",
        NOW.replace(hour=dep_hour),
        NOW.replace(hour=arr_hour),
        source=TRIPIT,
        tripit_segment_id="synthetic-transport",
    )
    itinerary = itinerary_for(
        merge_flights([flight]),
        schedule("2020-07-13T09:00:00Z") if crosses_window else schedule(),
        home_address="Grounded Philadelphia home",
        home_airports=frozenset({"PHL"}),
        attendances=(attendance,),
    )
    blocks, skipped, held = attendance_desired_blocks(
        (attendance,),
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        window=timedelta(days=2),
    )
    result = build_reconcile_plan(
        flights=[flight],
        airport_info={},
        current_blocks=[retained],
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        extra_desired=blocks,
        extra_held=held,
    )
    assert not result.plan.deletes
    if crosses_window:
        assert itinerary.position_at(NOW.replace(hour=17)).source == "home"
        assert any(h.kind == "attendance_return" for h in held), skipped
        assert not any(b.kind == "attendance_return" for b in blocks)
        assert not any(c.desired.kind == "attendance_return" for c in result.plan.creates)
        assert not any(u.desired.kind == "attendance_return" for u in result.plan.updates)
    else:
        assert not any(h.kind == "attendance_return" for h in result.held)
        returning = next(b for b in blocks if b.kind == "attendance_return")
        assert returning.origin == VENUE
        assert returning.destination == itinerary.base_at(returning.end).address
        [update] = [u for u in result.plan.updates if u.desired.kind == "attendance_return"]
        assert update.desired == returning


def measured_ev_adjuster(monkeypatch, blocks):
    from ev_charging import EnergySample
    from reconcile_sweep import _ev_adjuster
    from tesla_planning import TeslaPlanning

    # Only the read-provider boundary is synthetic. The actual sweep adapter,
    # cumulative energy owner, final engine checks and reconcile all execute.
    monkeypatch.setattr(TeslaPlanning, "vehicle", lambda *_: "Synthetic car")
    monkeypatch.setattr(TeslaPlanning, "navigation", lambda *_: ())
    monkeypatch.setattr(
        TeslaPlanning,
        "estimate",
        lambda *_: EnergySample("Synthetic car", 75, 0.18, NOW - timedelta(days=1), 90),
    )
    monkeypatch.setattr(
        TeslaPlanning, "unavailable", lambda *_: pytest.fail("Complete synthetic energy input")
    )
    return _ev_adjuster(
        {},
        lambda _a, _b: timedelta(minutes=20),
        {(b.origin, b.destination): 200 for b in blocks},
        NOW,
    )


@pytest.mark.parametrize("checkout", ["17:40:00", "18:40:00"])
def test_final_ev_return_arrival_rechecks_available_base(checkout, monkeypatch):
    from calendar_apply import build_create_args
    from engine import build_reconcile_plan

    attendance, itinerary = model(checkout=f"2020-07-13T{checkout}Z")
    blocks, _, held = attendance_desired_blocks(
        (attendance,),
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        window=timedelta(days=2),
    )
    assert held == []
    original = next(b for b in blocks if b.kind == "attendance_return")
    retained = parse_block(
        {"id": "retained-return", **build_create_args(original, calendar_id="primary")}
    )
    assert retained is not None
    adjust = measured_ev_adjuster(monkeypatch, blocks)
    measured, charging_held = adjust(blocks, [], [retained], [])
    assert charging_held == []
    final = next(b for b in measured if b.kind == "attendance_return")
    assert final.start == NOW.replace(hour=17)
    assert final.end == NOW.replace(hour=17, minute=50)
    assert final.charging_note == "1 charging stop, ~30 min"
    result = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=[retained],
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        extra_desired=blocks,
        adjust_drives=adjust,
    )
    assert not result.plan.deletes
    if checkout == "17:40:00":
        assert itinerary.base_at(final.end).address is None
        assert any(h.kind == "attendance_return" for h in result.held)
        assert not any(c.desired.kind == "attendance_return" for c in result.plan.creates)
        assert not any(u.desired.kind == "attendance_return" for u in result.plan.updates)
    else:
        assert itinerary.base_at(final.end).address == HOTEL
        assert not result.held
        [update] = [u for u in result.plan.updates if u.desired.kind == "attendance_return"]
        assert update.desired == final  # Never remove the actual charging allowance.


def test_legacy_meeting_ev_return_keeps_prior_checkout_semantics(monkeypatch):
    from calendar_apply import build_create_args
    from engine import build_reconcile_plan

    legacy = parse_attendance(
        {k: v for k, v in row().items() if k not in {"schema_version", "venue_location", "days"}}
    )
    itinerary = itinerary_for(
        [], schedule("2020-07-13T17:40:00Z"), home_address="Home", attendances=(legacy,)
    )
    meetings = scan(
        [event("legacy-outside", "Outside Office", 16, 17)],
        now=NOW,
        home_address="Home",
        attendances=(legacy,),
        anchor_for=lambda at: meeting_anchor(itinerary, at),
    )
    blocks, _, held = meeting_desired_blocks(meetings, route=lambda _a, _b: timedelta(minutes=20))
    assert held == []
    original = next(b for b in blocks if b.kind == "meeting_return")
    retained = parse_block(
        {"id": "legacy-return", **build_create_args(original, calendar_id="primary")}
    )
    assert retained is not None
    result = build_reconcile_plan(
        flights=[],
        airport_info={},
        current_blocks=[retained],
        itinerary=itinerary,
        route=lambda _a, _b: timedelta(minutes=20),
        now=NOW,
        extra_desired=blocks,
        adjust_drives=measured_ev_adjuster(monkeypatch, blocks),
    )
    assert result.held == () and result.plan.deletes == ()
    [update] = [u for u in result.plan.updates if u.desired.kind == "meeting_return"]
    assert update.desired.end == NOW.replace(hour=17, minute=50)
    assert update.desired.destination == HOTEL
    assert update.desired.charging_note == "1 charging stop, ~30 min"
    assert itinerary.position_at(update.desired.end).address == HOTEL


@pytest.mark.parametrize("layout", ["source", "native"])
def test_fresh_sweep_import_bootstraps_core_without_preloaded_sys_path(tmp_path, layout):
    import shutil
    import subprocess

    if layout == "source":
        script = ROOT / "skills/drive-engine/reconcile_sweep.py"
        expected_core = ROOT / "skills/travel-core/itinerary.py"
    else:
        # The only source transformation maps the fixed native prefix into a
        # private synthetic install. There is deliberately no sibling fallback.
        native = tmp_path / "native"
        scripts = native / "tessl__drive-engine"
        core = native / "tessl__travel-core"
        for bundle in ("drive-engine", "travel-core", "flight-assist"):
            target = native / f"tessl__{bundle}"
            target.mkdir(parents=True)
            for original in (ROOT / "skills" / bundle).glob("*.py"):
                shutil.copyfile(original, target / original.name)
            for path in target.glob("*.py"):
                path.write_text(path.read_text().replace("/home/node/.claude/skills", str(native)))
        script, expected_core = scripts / "reconcile_sweep.py", core / "itinerary.py"
    code = """import inspect,json,runpy,sys
scope=runpy.run_path(sys.argv[1],run_name='isolated_sweep_import')
print(json.dumps({'core':inspect.getfile(scope['Itinerary']),
                  'adapter':scope['attendance_desired_blocks'].__module__}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(script)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {
        "core": str(expected_core),
        "adapter": "attendance_source",
    }
