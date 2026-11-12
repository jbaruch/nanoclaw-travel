"""Tests for the talk-prep Prep-window adapter (#381).

The adapter executes another plugin's script, so the contract is pinned from
both ends: what it runs (only the fixed, regular, unsymlinked reader, with a
literal argv and a valid ID), how it bounds the child (exit, time, output), and
what it accepts back (exactly `{"hold": null}` or one tz-aware positive
interval). `prep_targets` adds the cost gate and staleness check the sweep uses.

The bounded-runner tests spawn tiny stand-in scripts written to `tmp_path`; the
lookup tests inject `run` the way `TeslaPlanning(run=...)` is tested.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

import prep_window  # noqa: E402
from prep_window import (  # noqa: E402
    NOT_INSTALLED,
    PrepHold,
    PrepWindows,
    PrepWindowUnavailable,
    parse_hold,
    parse_json,
    prep_targets,
    reader_path,
    run_reader,
)

TALK = datetime(2026, 11, 19, 21, 0, tzinfo=UTC)
PREP = datetime(2026, 11, 19, 20, 30, tzinfo=UTC)
HOLD = {"hold": {"id": "prephold1", "start": "2026-11-19T20:30:00Z", "end": "2026-11-19T21:00:00Z"}}


def _mount(tmp_path: Path) -> Path:
    mount = tmp_path.resolve() / "skills"
    scripts = mount / "tessl__talk-prep" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "talk-prep.py").write_text("# provider stand-in\n")
    return mount


class Spy:
    def __init__(self, answer: object = None, error: Exception | None = None):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, argv):
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        return self.answer


# --- what runs ---------------------------------------------------------------


def test_lookup_runs_the_fixed_reader_with_a_literal_argv(tmp_path):
    mount = _mount(tmp_path)
    spy = Spy(HOLD)
    hold = PrepWindows(run=spy, mount=mount).lookup("qcon_talk-1")
    assert hold == PrepHold(start=PREP, end=TALK)
    reader = mount / "tessl__talk-prep" / "scripts" / "talk-prep.py"
    assert spy.calls == [[sys.executable, str(reader), "--prep-window", "qcon_talk-1"]]


def test_the_production_reader_is_the_native_mount_path():
    assert prep_window.READER == Path(
        "/home/node/.claude/skills/tessl__talk-prep/scripts/talk-prep.py"
    )
    assert prep_window.TIMEOUT == 15


@pytest.mark.parametrize("event_id", ["", "has space", "semi;colon", "../x", "a" * 1025, "é"])
def test_an_id_the_provider_would_refuse_is_never_sent(tmp_path, event_id):
    spy = Spy(HOLD)
    assert PrepWindows(run=spy, mount=_mount(tmp_path)).lookup(event_id) is None
    assert spy.calls == []


def test_lookups_are_memoized_per_talk_within_a_sweep(tmp_path):
    spy = Spy(HOLD)
    windows = PrepWindows(run=spy, mount=_mount(tmp_path))
    assert windows.lookup("a") == windows.lookup("a")
    windows.lookup("b")
    assert [argv[-1] for argv in spy.calls] == ["a", "b"]


def test_a_failed_lookup_is_memoized_and_stays_unavailable(tmp_path):
    spy = Spy(error=PrepWindowUnavailable("talk-prep reader exited nonzero."))
    windows = PrepWindows(run=spy, mount=_mount(tmp_path))
    for _ in range(2):
        with pytest.raises(PrepWindowUnavailable):
            windows.lookup("a")
    assert len(spy.calls) == 1


def test_an_oserror_spawning_the_reader_is_unavailable(tmp_path):
    windows = PrepWindows(run=Spy(error=OSError("exec format error")), mount=_mount(tmp_path))
    with pytest.raises(PrepWindowUnavailable):
        windows.lookup("a")


# --- install states ----------------------------------------------------------


def test_not_installed_keeps_no_hold_with_one_diagnostic(tmp_path, capsys):
    mount = tmp_path.resolve() / "skills"
    mount.mkdir()
    spy = Spy(HOLD)
    windows = PrepWindows(run=spy, mount=mount)
    assert windows.lookup("a") is None
    assert windows.lookup("b") is None
    assert spy.calls == []
    assert windows.diagnostics == [NOT_INSTALLED]
    assert capsys.readouterr().err == NOT_INSTALLED + "\n"


def test_no_lookup_touches_nothing_and_prints_nothing(tmp_path, capsys):
    windows = PrepWindows(run=Spy(HOLD), mount=tmp_path / "missing")
    assert windows.diagnostics == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("missing", ["scripts", "talk-prep.py"])
def test_a_partial_install_is_unavailable_never_a_fallback(tmp_path, missing):
    mount = _mount(tmp_path)
    scripts = mount / "tessl__talk-prep" / "scripts"
    (scripts / "talk-prep.py").unlink()
    if missing == "scripts":
        scripts.rmdir()
    spy = Spy(HOLD)
    with pytest.raises(PrepWindowUnavailable):
        PrepWindows(run=spy, mount=mount).lookup("a")
    assert spy.calls == []


@pytest.mark.parametrize("linked", ["skill", "scripts", "reader"])
def test_a_symlinked_install_is_refused(tmp_path, linked):
    real = _mount(tmp_path / "real") / "tessl__talk-prep"
    mount = tmp_path.resolve() / "skills"
    if linked == "skill":
        mount.mkdir()
        os.symlink(real, mount / "tessl__talk-prep")
    elif linked == "scripts":
        (mount / "tessl__talk-prep").mkdir(parents=True)
        os.symlink(real / "scripts", mount / "tessl__talk-prep" / "scripts")
    else:
        (mount / "tessl__talk-prep" / "scripts").mkdir(parents=True)
        os.symlink(
            real / "scripts" / "talk-prep.py",
            mount / "tessl__talk-prep" / "scripts" / "talk-prep.py",
        )
    with pytest.raises(PrepWindowUnavailable):
        reader_path(mount)
    spy = Spy(HOLD)
    with pytest.raises(PrepWindowUnavailable):
        PrepWindows(run=spy, mount=mount).lookup("a")
    assert spy.calls == []


def test_a_dangling_skill_symlink_counts_as_present_and_is_refused(tmp_path):
    mount = tmp_path.resolve() / "skills"
    mount.mkdir()
    os.symlink(tmp_path / "nowhere", mount / "tessl__talk-prep")
    with pytest.raises(PrepWindowUnavailable):
        reader_path(mount)


# --- what it accepts ---------------------------------------------------------


def test_a_null_hold_is_no_hold():
    assert parse_hold({"hold": None}) is None


def test_a_hold_in_another_offset_is_the_same_instant():
    hold = parse_hold(
        {
            "hold": {
                "id": "h",
                "start": "2026-11-19T12:30:00-08:00",
                "end": "2026-11-19T13:00:00-08:00",
            }
        }
    )
    assert hold == PrepHold(start=PREP, end=TALK)


@pytest.mark.parametrize(
    "answer",
    [
        [],
        None,
        {},
        {"hold": None, "extra": 1},
        {"Hold": None},
        {"hold": "2026-11-19T20:30:00Z"},
        {"hold": {"id": "h", "start": "2026-11-19T20:30:00Z"}},
        {"hold": {"id": "h", "start": "2026-11-19T20:30:00Z", "end": TALK.isoformat(), "x": 1}},
        {"hold": {"id": "", "start": "2026-11-19T20:30:00Z", "end": "2026-11-19T21:00:00Z"}},
        {"hold": {"id": 7, "start": "2026-11-19T20:30:00Z", "end": "2026-11-19T21:00:00Z"}},
        {"hold": {"id": "h", "start": "2026-11-19T20:30:00", "end": "2026-11-19T21:00:00"}},
        {"hold": {"id": "h", "start": "2026-11-19", "end": "2026-11-20"}},
        {"hold": {"id": "h", "start": "soon", "end": "2026-11-19T21:00:00Z"}},
        {"hold": {"id": "h", "start": 1, "end": "2026-11-19T21:00:00Z"}},
        {"hold": {"id": "h", "start": "2026-11-19T21:00:00Z", "end": "2026-11-19T21:00:00Z"}},
        {"hold": {"id": "h", "start": "2026-11-19T21:30:00Z", "end": "2026-11-19T21:00:00Z"}},
    ],
)
def test_an_off_contract_answer_is_unavailable(answer):
    with pytest.raises(PrepWindowUnavailable):
        parse_hold(answer)


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b'{"hold": null, "hold": null}', b"\xff\xfe", b"[" * 100000],
)
def test_invalid_or_duplicate_key_json_is_unavailable(raw):
    with pytest.raises(PrepWindowUnavailable):
        parse_json(raw)


# --- the bounded runner (real child processes) -------------------------------


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "stand_in.py"
    path.write_text(body)
    return [sys.executable, str(path)]


def test_the_runner_returns_the_childs_json(tmp_path):
    argv = _script(tmp_path, f"print({json.dumps(json.dumps(HOLD))})\n")
    assert parse_hold(run_reader(argv)) == PrepHold(start=PREP, end=TALK)


def test_a_nonzero_exit_is_unavailable_even_with_a_null_answer(tmp_path):
    argv = _script(tmp_path, "import sys\nprint('{\"hold\": null}')\nsys.exit(1)\n")
    with pytest.raises(PrepWindowUnavailable, match="nonzero"):
        run_reader(argv)


def test_an_argparse_refusal_is_unavailable(tmp_path):
    argv = _script(tmp_path, "import sys\nsys.stderr.write('usage: bad id\\n')\nsys.exit(2)\n")
    with pytest.raises(PrepWindowUnavailable):
        run_reader(argv)


def test_a_hung_reader_is_killed_at_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(prep_window, "TIMEOUT", 0.5)
    argv = _script(tmp_path, "import time\ntime.sleep(30)\n")
    started = datetime.now(UTC)
    with pytest.raises(PrepWindowUnavailable, match="bounded wait"):
        run_reader(argv)
    assert datetime.now(UTC) - started < timedelta(seconds=10)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_oversize_output_on_either_stream_is_unavailable(tmp_path, stream):
    argv = _script(
        tmp_path, f"import sys\nsys.{stream}.write('x' * {prep_window.MAX_OUTPUT + 10})\n"
    )
    with pytest.raises(PrepWindowUnavailable, match="bound"):
        run_reader(argv)


def test_invalid_json_from_the_child_is_unavailable(tmp_path):
    argv = _script(tmp_path, "print('hold: none')\n")
    with pytest.raises(PrepWindowUnavailable, match="invalid JSON"):
        run_reader(argv)


# --- prep_targets: the sweep's gate and staleness check ----------------------


@dataclass
class Leg:
    direction: str


@dataclass
class Meeting:
    meeting_id: str
    start: datetime | None
    legs: tuple = (Leg("outbound"), Leg("return"))


def _event(event_id, start, end, **extra):
    return {
        "id": event_id,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        **extra,
    }


TALK_EVENT = _event("qcon", TALK, TALK + timedelta(hours=2))
PREP_EVENT = _event("prephold1", PREP, TALK)


def test_a_hold_ending_at_the_talk_start_retargets(tmp_path):
    spy = Spy(HOLD)
    windows = PrepWindows(run=spy, mount=_mount(tmp_path))
    arrival, unavailable = prep_targets([Meeting("qcon", TALK)], [TALK_EVENT, PREP_EVENT], windows)
    assert arrival == {"qcon": PREP} and unavailable == frozenset()
    assert [argv[-1] for argv in spy.calls] == ["qcon"]


def test_a_bridge_leg_is_gated_in_too(tmp_path):
    spy = Spy(HOLD)
    arrival, _ = prep_targets(
        [Meeting("qcon", TALK, (Leg("bridge"),))],
        [TALK_EVENT, PREP_EVENT],
        PrepWindows(run=spy, mount=_mount(tmp_path)),
    )
    assert arrival == {"qcon": PREP}


@pytest.mark.parametrize(
    ("meeting", "events"),
    [
        # Nothing abuts the talk: the reader is never asked.
        (Meeting("qcon", TALK), [TALK_EVENT]),
        (Meeting("qcon", TALK), [TALK_EVENT, _event("lunch", PREP, TALK - timedelta(minutes=5))]),
        # Only an event carrying the talk's own id ends at the talk start.
        (Meeting("qcon", TALK), [TALK_EVENT, _event("qcon", PREP, TALK)]),
        # A cancelled or all-day abutting event is no Prep hold.
        (Meeting("qcon", TALK), [TALK_EVENT, _event("x", PREP, TALK, status="cancelled")]),
        (
            Meeting("qcon", TALK),
            [
                TALK_EVENT,
                {"id": "x", "start": {"date": "2026-11-19"}, "end": {"date": "2026-11-20"}},
            ],
        ),
        # No arriving leg (back-to-back, return only) or no start: nothing to retarget.
        (Meeting("qcon", TALK, (Leg("return"),)), [TALK_EVENT, PREP_EVENT]),
        (Meeting("qcon", TALK, ()), [TALK_EVENT, PREP_EVENT]),
        (Meeting("qcon", None), [TALK_EVENT, PREP_EVENT]),
    ],
)
def test_the_reader_is_not_called_without_a_gate_candidate(tmp_path, meeting, events):
    spy = Spy(HOLD)
    arrival, unavailable = prep_targets(
        [meeting], events, PrepWindows(run=spy, mount=_mount(tmp_path))
    )
    assert spy.calls == []
    assert arrival == {} and unavailable == frozenset()


def test_an_abutting_event_in_another_offset_still_gates(tmp_path):
    pst = TALK.astimezone(datetime.fromisoformat("2026-11-19T00:00:00-08:00").tzinfo)
    spy = Spy(HOLD)
    arrival, _ = prep_targets(
        [Meeting("qcon", TALK)],
        [TALK_EVENT, _event("prephold1", PREP, pst)],
        PrepWindows(run=spy, mount=_mount(tmp_path)),
    )
    assert arrival == {"qcon": PREP}


def test_a_null_hold_keeps_the_talk_start(tmp_path):
    arrival, unavailable = prep_targets(
        [Meeting("qcon", TALK)],
        [TALK_EVENT, PREP_EVENT],
        PrepWindows(run=Spy({"hold": None}), mount=_mount(tmp_path)),
    )
    assert arrival == {} and unavailable == frozenset()


def test_a_moved_talk_follows_the_repaired_hold(tmp_path):
    moved = TALK + timedelta(hours=1)
    repaired = {
        "hold": {"id": "prephold1", "start": "2026-11-19T21:30:00Z", "end": "2026-11-19T22:00:00Z"}
    }
    arrival, _ = prep_targets(
        [Meeting("qcon", moved)],
        [_event("qcon", moved, moved + timedelta(hours=2)), _event("prephold1", PREP, moved)],
        PrepWindows(run=Spy(repaired), mount=_mount(tmp_path)),
    )
    assert arrival == {"qcon": moved - timedelta(minutes=30)}


def test_a_stale_hold_keeps_the_talk_start_with_a_diagnostic(tmp_path, capsys):
    # The talk moved to 14:00; something else ends at 14:00, but the hold the
    # reader returns still ends at the old 13:00 start.
    moved = TALK + timedelta(hours=1)
    windows = PrepWindows(run=Spy(HOLD), mount=_mount(tmp_path))
    arrival, unavailable = prep_targets(
        [Meeting("qcon", moved)],
        [_event("qcon", moved, moved + timedelta(hours=2)), _event("lunch", TALK, moved)],
        windows,
    )
    assert arrival == {} and unavailable == frozenset()
    assert len(windows.diagnostics) == 1
    assert "no longer ends at the talk start" in windows.diagnostics[0]
    assert "qcon" in capsys.readouterr().err


def test_a_failed_read_marks_the_talk_unavailable_with_a_diagnostic(tmp_path, capsys):
    windows = PrepWindows(
        run=Spy(error=PrepWindowUnavailable("talk-prep reader exited nonzero.")),
        mount=_mount(tmp_path),
    )
    arrival, unavailable = prep_targets([Meeting("qcon", TALK)], [TALK_EVENT, PREP_EVENT], windows)
    assert arrival == {} and unavailable == frozenset({"qcon"})
    assert "Prep window unavailable for meeting qcon" in capsys.readouterr().err


def test_not_installed_keeps_every_talk_start_with_exactly_one_diagnostic(tmp_path, capsys):
    mount = tmp_path.resolve() / "skills"
    mount.mkdir()
    other_talk = TALK + timedelta(days=1)
    spy = Spy(HOLD)
    arrival, unavailable = prep_targets(
        [Meeting("qcon", TALK), Meeting("qcon2", other_talk)],
        [
            TALK_EVENT,
            PREP_EVENT,
            _event("qcon2", other_talk, other_talk + timedelta(hours=1)),
            _event("prep2", other_talk - timedelta(minutes=30), other_talk),
        ],
        PrepWindows(run=spy, mount=mount),
    )
    assert arrival == {} and unavailable == frozenset()
    assert spy.calls == []
    assert capsys.readouterr().err == NOT_INSTALLED + "\n"
