"""Grounded attendance planner inputs and real no-drive pipeline regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for bundle in ("travel-core", "flight-assist", "drive-engine"):
    sys.path.insert(0, str(ROOT / "skills" / bundle))

from calendar_apply import apply_plan  # noqa: E402
from conference_attendance import (  # noqa: E402
    AttendanceError,
    decode_store,
    load_attendances,
    parse_attendance,
    record_attendance,
)
from itinerary import Itinerary  # noqa: E402
from meeting_source import meeting_desired_blocks  # noqa: E402
from postarrival import postarrival_meetings_from  # noqa: E402
from reconcile import plan_reconcile  # noqa: E402
from reconcile_sweep import build_sweep_payload, meeting_anchor  # noqa: E402
from scan import scan  # noqa: E402

NOW = datetime(2020, 7, 13, 9, tzinfo=UTC)
LABEL = "ExampleConf Denver"


def _row(**changes):
    return {
        "id": "example-2020",
        "starts_on": "2020-07-13",
        "ends_on": "2020-07-14",
        "time_zone": "America/Denver",
        "venue_locations": [LABEL, "Conference Center Address"],
        **changes,
    }


def _event(identity: object = "native-m1", location=f"Booth A - {LABEL}", *, start=None, end=None):
    start = start or NOW.replace(hour=16)
    return {
        "id": identity,
        "summary": "Unrelated title",
        "location": location,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (end or start + timedelta(hours=1)).isoformat()},
    }


def _payload(row):
    return json.dumps({"schema_version": 1, "attendances": [row]}).encode()


class Calendar:
    def __init__(self):
        self.created = []

    def create_event(self, args):
        self.created.append(args)
        return {"id": "created"}


@pytest.mark.parametrize("lodging", [False, True])
def test_configured_conference_new_booths_need_no_route_block_or_notice(lodging):
    schedule = [
        {
            "type": "Trip",
            "summary": LABEL,
            "location": "Denver, CO",
            "start": "2020-07-12",
            "end": "2020-07-15",
        },
        {
            "type": "Flight",
            "summary": "WN1 BNA to DEN",
            "start": "2020-07-12T17:20:00Z",
            "end": "2020-07-12T22:10:00Z",
        },
    ]
    if lodging:
        schedule += [
            {
                "type": "Lodging",
                "summary": "Check-in: Hotel",
                "location": "Hotel Address",
                "start": "2020-07-12T15:00:00Z",
            },
            {
                "type": "Lodging",
                "summary": "Check-out: Hotel",
                "location": "Hotel Address",
                "start": "2020-07-15T15:00:00Z",
            },
        ]
    itinerary = Itinerary(schedule, home_address="Home", home_airports=frozenset({"BNA"}))
    events = [_event(), _event("native-new", f"New Booth - {LABEL}", start=NOW.replace(hour=18))]
    meetings = scan(
        events,
        now=NOW,
        home_address="Home",
        attendances=(parse_attendance(_row()),),
        anchor_for=lambda at: meeting_anchor(itinerary, at),
    )
    calls = []

    def route(origin, destination):
        calls.append((origin, destination))
        return timedelta(minutes=20)

    desired, skipped, held = meeting_desired_blocks(meetings, route=route)
    plan = plan_reconcile(desired, [], held=frozenset(h.key for h in held), now=NOW)
    calendar = Calendar()
    applied = apply_plan(plan, calendar=calendar, now=NOW, creation_clock=lambda: NOW)
    assert calls == desired == held == calendar.created == []
    assert build_sweep_payload(applied, skipped)["data"]["message"] is None
    assert postarrival_meetings_from(meetings, now=NOW) == []


@pytest.mark.parametrize(
    "location",
    [
        "Denver",
        "ExampleConf Denver Extra",
        "OtherConf Denver",
        "Booth - ExampleConf",
        "Unlisted Hall",
        "Booth - NotExampleConf Denver",
        "https://example.test/ExampleConf Denver",
    ],
)
def test_unapproved_alias_city_substring_and_similar_conference_do_not_suppress(location):
    meetings = scan(
        [_event(location=location)],
        now=NOW,
        home_address="Home",
        attendances=(parse_attendance(_row()),),
    )
    # A URL is independently virtual, never an attendance match.
    assert meetings[0].attendance_id is None
    if not location.startswith("https:"):
        assert len(meetings[0].legs) == 2


@pytest.mark.parametrize(
    "changes", [{"starts_on": "2020-07-14"}, {"ends_on": "2020-07-12", "starts_on": "2020-07-12"}]
)
def test_outside_attendance_days_keeps_ordinary_drives(changes):
    meetings = scan(
        [_event()], now=NOW, home_address="Home", attendances=(parse_attendance(_row(**changes)),)
    )
    assert len(meetings[0].legs) == 2


def test_full_local_span_and_native_id_are_required():
    attendance = parse_attendance(_row())
    assert not attendance.covers("", LABEL, NOW, NOW + timedelta(hours=1))
    # UTC July 15 05:00 is still July 14 in Denver; end exactly midnight is covered.
    assert attendance.covers(
        "native", LABEL, datetime(2020, 7, 15, 5, tzinfo=UTC), datetime(2020, 7, 15, 6, tzinfo=UTC)
    )
    assert not attendance.covers(
        "native",
        LABEL,
        datetime(2020, 7, 15, 5, tzinfo=UTC),
        datetime(2020, 7, 15, 6, 1, tzinfo=UTC),
    )


def test_real_external_bridges_and_return_survive_onsite_neighbor():
    events = [
        _event("onsite", start=NOW.replace(hour=12), end=NOW.replace(hour=13)),
        _event(
            "external",
            "Outside Office",
            start=NOW.replace(hour=13, minute=30),
            end=NOW.replace(hour=14),
        ),
        _event("onsite-next", f"Other Booth - {LABEL}", start=NOW.replace(hour=14, minute=30)),
    ]
    meetings = scan(events, now=NOW, home_address="Hotel", attendances=(parse_attendance(_row()),))
    desired, _, held = meeting_desired_blocks(meetings, route=lambda _o, _d: timedelta(minutes=10))
    assert held == []
    assert [(b.identity, b.origin, b.destination) for b in desired] == [
        ("external", f"Booth A - {LABEL}", "Outside Office"),
        ("onsite-next", "Outside Office", f"Other Booth - {LABEL}"),
    ]
    # A final external appointment still owns its normal return to lodging.
    meetings = scan(
        events[:2], now=NOW, home_address="Hotel", attendances=(parse_attendance(_row()),)
    )
    desired, _, _ = meeting_desired_blocks(meetings, route=lambda _o, _d: timedelta(minutes=10))
    assert [(b.kind, b.destination) for b in desired] == [
        ("meeting_outbound", "Outside Office"),
        ("meeting_return", "Hotel"),
    ]


def test_store_upsert_missing_future_and_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path))
    assert load_attendances() == ()
    record_attendance(_row())
    record_attendance(_row(id="other", starts_on="2020-08-01", ends_on="2020-08-02"))
    assert len(load_attendances()) == 2
    path = tmp_path / "conference-attendance.json"
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('{"schema_version":2,"attendances":[]}')
    with pytest.raises(AttendanceError):
        record_attendance(_row())
    assert json.loads(path.read_text())["schema_version"] == 2
    result = subprocess.run(
        [sys.executable, str(ROOT / "skills/drive-engine/record_attendance.py")],
        input=_payload(_row()),
        capture_output=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["recorded"] is False
    path.unlink()
    result = subprocess.run(
        [sys.executable, str(ROOT / "skills/drive-engine/record_attendance.py")],
        input=_payload(_row()),
        capture_output=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["recorded"] is True
    assert load_attendances() == (parse_attendance(_row()),)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1,"attendances":[]}',
        b'{"schema_version":true,"attendances":[]}',
        b"[]",
        b"bad",
        b"x" * (256 * 1024 + 1),
    ],
)
def test_malformed_store_is_never_empty(raw):
    with pytest.raises(AttendanceError):
        decode_store(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"time_zone": "Not/AZone"},
        {"starts_on": "20200713"},
        {"venue_locations": ["A", " a "]},
        {"venue_locations": ["a\n"]},
        {"ends_on": "9999-12-31"},
        {"unexpected": True},
    ],
)
def test_invalid_rows_are_refused(changes):
    with pytest.raises(AttendanceError):
        parse_attendance(_row(**changes))


def test_missing_or_nonstring_native_ids_never_establish_presence():
    for identity in (None, 42, "", "native\n"):
        meetings = scan(
            [_event(identity)],
            now=NOW,
            home_address="Home",
            attendances=(parse_attendance(_row()),),
        )
        assert meetings[0].attendance_id is None
        assert len(meetings[0].legs) == 2


def test_withdrawn_labels_and_symlinked_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path))
    record_attendance(_row())
    record_attendance(_row(venue_locations=[]))
    assert not load_attendances()[0].covers("native", LABEL, NOW, NOW + timedelta(hours=1))
    path = tmp_path / "conference-attendance.json"
    path.rename(tmp_path / "other")
    path.symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        load_attendances()
    with pytest.raises(OSError):
        record_attendance(_row())


def test_ambiguous_matching_occurrences_hold_instead_of_routing():
    rows = (parse_attendance(_row()), parse_attendance(_row(id="other")))
    with pytest.raises(AttendanceError):
        scan([_event()], now=NOW, home_address="Home", attendances=rows)


def test_attendance_does_not_override_decline_cancel_flight_or_skip():
    for extra in (
        {"status": "cancelled"},
        {"attendees": [{"self": True, "responseStatus": "declined"}]},
        {"summary": "Flight to DEN"},
    ):
        event = {**_event(), **extra}
        result = scan(
            [event], now=NOW, home_address="Home", attendances=(parse_attendance(_row()),)
        )
        assert result[0].bucket == "filtered"
        assert result[0].legs == ()
    result = scan(
        [_event()],
        now=NOW,
        home_address="Home",
        attendances=(parse_attendance(_row()),),
        skip_state={"native-m1": "2020-07-14T00:00:00Z"},
    )
    assert result[0].bucket == "skipped"


def test_onsite_neighbor_keeps_postarrival_external_return_suppressed():
    past = _event(
        "external",
        "Outside Office",
        start=NOW - timedelta(hours=1),
        end=NOW + timedelta(minutes=30),
    )
    onsite = _event("onsite", start=NOW + timedelta(hours=1))
    meetings = scan(
        [past, onsite], now=NOW, home_address="Hotel", attendances=(parse_attendance(_row()),)
    )
    lifted = postarrival_meetings_from(meetings, now=NOW)
    assert len(lifted) == 1
    assert lifted[0].identity == "external"
    assert lifted[0].return_suppressed is True


def test_new_store_namespace_does_not_adopt_legacy_file(tmp_path, monkeypatch):
    legacy, current = tmp_path / "legacy", tmp_path / "engine"
    legacy.mkdir()
    (legacy / "conference-attendance.json").write_bytes(_payload(_row()))
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(legacy))
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(current))
    assert load_attendances() == ()
    record_attendance(_row())
    assert (current / "conference-attendance.json").is_file()
    assert (legacy / "conference-attendance.json").read_bytes() == _payload(_row())


def test_writer_verifies_numeric_owner_and_modes_on_new_and_replaced_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path / "engine"))
    for row in (_row(), _row(venue_locations=["Replacement Label"])):
        record_attendance(row)
        for name, mode in (
            ("", 0o700),
            (".conference-attendance.lock", 0o600),
            ("conference-attendance.json", 0o600),
        ):
            info = (tmp_path / "engine" / name).stat()
            assert (info.st_uid, info.st_gid) == (os.geteuid(), os.getegid())
            assert info.st_mode & 0o777 == mode
        assert load_attendances() == (parse_attendance(row),)


def test_root_writer_refuses_before_creating_any_state(tmp_path, monkeypatch):
    import conference_attendance as owner

    directory = tmp_path / "engine"
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(directory))
    monkeypatch.setattr(owner.os, "geteuid", lambda: 0)
    with pytest.raises(AttendanceError, match="Refusing root"):
        record_attendance(_row())
    assert not directory.exists()


def test_lock_mode_and_multiply_linked_state_are_refused_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path))
    lock = tmp_path / ".conference-attendance.lock"
    lock.touch(mode=0o644)
    with pytest.raises(AttendanceError, match="ownership/mode/type"):
        record_attendance(_row())
    assert not (tmp_path / "conference-attendance.json").exists()
    lock.chmod(0o600)
    record_attendance(_row())
    path = tmp_path / "conference-attendance.json"
    original = path.read_bytes()
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(AttendanceError):
        record_attendance(_row(venue_locations=["Changed"]))
    assert path.read_bytes() == original


@pytest.mark.parametrize("fault", ["mode", "uid", "gid"])
def test_final_reopened_descriptor_mismatch_never_reports_success(tmp_path, monkeypatch, fault):
    import conference_attendance as owner

    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path))
    record_attendance(_row())
    real_replace, real_fstat = owner.os.replace, owner.os.fstat
    replaced = False

    def replace(*args, **kwargs):
        nonlocal replaced
        real_replace(*args, **kwargs)
        replaced = True
        if fault == "mode":
            (tmp_path / "conference-attendance.json").chmod(0o644)

    def fstat(fd):
        info = real_fstat(fd)
        final = (tmp_path / "conference-attendance.json").stat()
        if replaced and info.st_ino == final.st_ino and fault in {"uid", "gid"}:
            values = list(info)
            values[4 if fault == "uid" else 5] += 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(owner.os, "replace", replace)
    monkeypatch.setattr(owner.os, "fstat", fstat)
    with pytest.raises(AttendanceError, match="ownership/mode/type"):
        record_attendance(_row(venue_locations=["Updated"]))
    assert not list(tmp_path.glob(".conference-attendance.*[0-9a-f]"))


def test_cli_diagnostics_distinguish_input_state_permission_and_write_failure(
    tmp_path, monkeypatch, capsys
):
    import io

    import record_attendance as cli

    monkeypatch.setenv("DRIVE_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli.sys, "argv", ["record_attendance.py"])

    def invoke(raw):
        monkeypatch.setattr(cli.sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))
        assert cli.main() == 1
        output = capsys.readouterr()
        assert json.loads(output.out)["recorded"] is False
        assert "private-value" not in output.out + output.err
        return output.err

    assert "phase=request_decode" in invoke(b"private-value")
    path = tmp_path / "conference-attendance.json"
    path.write_bytes(b"private-value")
    path.chmod(0o600)
    assert "phase=state_upsert" in invoke(_payload(_row()))
    assert path.read_bytes() == b"private-value"
    for failure, expected in (
        (PermissionError(13, "private-value"), "errno=13"),
        (OSError(5, "private-value"), "errno=5"),
        (RuntimeError("private-value"), "cause=RuntimeError"),
    ):

        def fail(_row, problem=failure):
            raise problem

        monkeypatch.setattr(cli, "record_attendance", fail)
        assert expected in invoke(_payload(_row()))


@pytest.mark.parametrize("character", ["\x7f", "\x85", "\u200d", "\ud800", "\u0378"])
@pytest.mark.parametrize("field", ["id", "venue_locations"])
def test_persisted_controls_and_invalid_unicode_are_refused(character, field):
    value = "bad" + character
    changes = {field: [value] if field == "venue_locations" else value}
    with pytest.raises(AttendanceError):
        parse_attendance(_row(**changes))


@pytest.mark.parametrize("attended", [False, True])
def test_ongoing_attendance_removes_phantom_return_but_preserves_drive_history(attended):
    from block_codec import GEN_UNIFIED, ParsedBlock

    start, end = NOW - timedelta(hours=1), NOW + timedelta(hours=1)
    meetings = scan(
        [_event(start=start, end=end)],
        now=NOW,
        home_address="Hotel",
        attendances=(parse_attendance(_row()),) if attended else (),
    )
    desired, _, held = meeting_desired_blocks(meetings, route=lambda _o, _d: timedelta(minutes=20))
    current = [
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="historic-out",
            identity="native-m1",
            kind="meeting_outbound",
            baseline_seconds=1200,
            anchor=start,
            origin="Hotel",
            destination=LABEL,
        ),
        ParsedBlock(
            generation=GEN_UNIFIED,
            event_id="phantom-return",
            identity="native-m1",
            kind="meeting_return",
            baseline_seconds=1200,
            anchor=end,
            origin=LABEL,
            destination="Hotel",
        ),
    ]
    plan = plan_reconcile(desired, current, held=frozenset(h.key for h in held), now=NOW)
    assert plan.creates == plan.updates == ()
    assert [operation.event_id for operation in plan.deletes] == (
        ["phantom-return"] if attended else []
    )
    assert len(held) == (0 if attended else 2)
