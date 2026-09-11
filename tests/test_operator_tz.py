"""Tests for `travel-core/operator_tz.py` — the door to the core zone reader.

The reader itself is `jbaruch/nanoclaw-core`'s and is not in this tree, so the
subprocess is faked: `run` is injected and returns a `CompletedProcess` built
from the reader's documented output shapes, and `reader` points at a tmp file
so the "installed" check passes. Deterministic — fixed instants, no wall-clock.

Pins:
  - an `available: true` payload becomes an `OperatorTz`
  - every unavailable shape (reader missing, `available: false`, exit 1 store
    failure, exit 2 misuse, timeout, OSError, non-JSON, missing fields) is None
    with a stderr line, and the reader's own stderr is relayed
  - `--now` rides through to the reader argv; a naive `now` is rejected
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))

from operator_tz import (  # noqa: E402
    CORE_READER_PATH,
    READER_TIMEOUT_SECONDS,
    OperatorTz,
    read_operator_tz,
)

AVAILABLE = (
    '{"available":true,"tz":"America/Chicago",'
    '"local_now":"2026-09-10T12:03:00-05:00","local_date":"2026-09-10"}\n'
)
UNAVAILABLE = '{"available":false,"tz":null,"local_now":null,"local_date":null}\n'


class FakeRun:
    """Records the argv it was called with; answers with the configured process."""

    def __init__(self, *, returncode=0, stdout="", stderr="", raises=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def reader(tmp_path: Path) -> Path:
    script = tmp_path / "read-current-tz.py"
    script.write_text("# stand-in for the core reader; never executed\n")
    return script


def test_available_payload_becomes_operator_tz(reader, capsys):
    run = FakeRun(stdout=AVAILABLE)
    zone = read_operator_tz(reader=reader, run=run)
    assert zone == OperatorTz(
        tz="America/Chicago", local_now="2026-09-10T12:03:00-05:00", local_date="2026-09-10"
    )
    assert capsys.readouterr().err == ""


def test_reader_is_spawned_with_the_interpreter_and_no_now_by_default(reader):
    run = FakeRun(stdout=AVAILABLE)
    read_operator_tz(reader=reader, run=run)
    [(argv, kwargs)] = run.calls
    assert argv == [sys.executable, str(reader)]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == READER_TIMEOUT_SECONDS
    assert kwargs["check"] is False


def test_now_rides_through_as_the_reader_now_flag(reader):
    run = FakeRun(stdout=AVAILABLE)
    at = datetime(2026, 9, 10, 19, 50, tzinfo=timezone(timedelta(hours=-5)))
    read_operator_tz(now=at, reader=reader, run=run)
    [(argv, _)] = run.calls
    assert argv[2:] == ["--now", "2026-09-10T19:50:00-05:00"]


class _NoOffset(tzinfo):
    """A tzinfo that never yields an offset — Python's definition of naive."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "no-offset"


@pytest.mark.parametrize(
    "naive",
    [
        datetime(2026, 9, 10, 19, 50),
        datetime(2026, 9, 10, 19, 50, tzinfo=_NoOffset()),
    ],
)
def test_naive_now_is_rejected_before_spawning(reader, naive):
    run = FakeRun(stdout=AVAILABLE)
    with pytest.raises(ValueError, match="timezone-aware"):
        read_operator_tz(now=naive, reader=reader, run=run)
    assert run.calls == []


def test_missing_reader_is_unavailable_without_spawning(tmp_path, capsys):
    run = FakeRun(stdout=AVAILABLE)
    absent = tmp_path / "nope" / "read-current-tz.py"
    assert read_operator_tz(reader=absent, run=run) is None
    assert run.calls == []
    err = capsys.readouterr().err
    assert str(absent) in err
    assert "jbaruch/nanoclaw-core" in err


def test_default_reader_is_the_runtime_mount():
    assert CORE_READER_PATH == Path(
        "/home/node/.claude/skills/tessl__current-tz/scripts/read-current-tz.py"
    )


def test_unavailable_shape_is_none_and_relays_reader_stderr(reader, capsys):
    run = FakeRun(stdout=UNAVAILABLE, stderr="read-current-tz: tz_state has no singleton row")
    assert read_operator_tz(reader=reader, run=run) is None
    err = capsys.readouterr().err
    assert "tz_state has no singleton row" in err
    assert "available: false" in err
    assert err.endswith("\n")


def test_unavailable_shape_with_a_silent_reader_still_says_so(reader, capsys):
    """The miss must be visible even when the reader writes nothing to stderr."""
    run = FakeRun(stdout=UNAVAILABLE, stderr="")
    assert read_operator_tz(reader=reader, run=run) is None
    assert "operator zone unavailable" in capsys.readouterr().err


def test_store_unreadable_exit_1_is_none(reader, capsys):
    run = FakeRun(
        returncode=1, stdout=UNAVAILABLE, stderr="read-current-tz: cannot read tz_state\n"
    )
    assert read_operator_tz(reader=reader, run=run) is None
    assert "cannot read tz_state" in capsys.readouterr().err


def test_cli_misuse_exit_2_is_none_with_a_diagnostic(reader, capsys):
    run = FakeRun(returncode=2, stdout="", stderr="usage: read-current-tz.py [--now NOW]\n")
    at = datetime(2026, 9, 10, 19, 50, tzinfo=timezone.utc)
    assert read_operator_tz(now=at, reader=reader, run=run) is None
    err = capsys.readouterr().err
    assert "exited 2" in err
    assert "--now" in err


def test_timeout_is_none_with_a_diagnostic(reader, capsys):
    run = FakeRun(raises=subprocess.TimeoutExpired(cmd="reader", timeout=5))
    assert read_operator_tz(reader=reader, run=run) is None
    assert "did not answer" in capsys.readouterr().err


def test_spawn_failure_is_none_with_a_diagnostic(reader, capsys):
    run = FakeRun(raises=OSError("exec format error"))
    assert read_operator_tz(reader=reader, run=run) is None
    assert "exec format error" in capsys.readouterr().err


@pytest.mark.parametrize(
    "stdout",
    [
        "not json at all\n",
        "[1, 2, 3]\n",
        '{"available":true,"tz":"America/Chicago","local_now":null,"local_date":"2026-09-10"}\n',
        '{"available":true,"tz":"","local_now":"x","local_date":"y"}\n',
    ],
)
def test_unparseable_or_incomplete_payload_is_none_with_a_diagnostic(reader, capsys, stdout):
    run = FakeRun(stdout=stdout)
    assert read_operator_tz(reader=reader, run=run) is None
    assert "operator zone unavailable" in capsys.readouterr().err
