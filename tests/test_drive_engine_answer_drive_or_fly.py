"""Tests for the drive-or-fly answer handler.

The store is redirected to a tmp_path and the schedule is injected, so nothing
reads the deployed state or the live feed. `now` is always passed in.

What matters here is the CLI contract the skill depends on: stdout is always
parseable JSON, an unresolved trip is a result rather than a crash, and the
recorded answer is the one the next sweep will honour.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from answer_drive_or_fly import main, record_answer  # noqa: E402
from drive_decision import (  # noqa: E402
    DECIDED_BY_OPERATOR,
    VERDICT_DRIVE,
    VERDICT_FLY,
    load_verdicts,
)

UTC = timezone.utc
NOW = datetime(2020, 8, 7, 12, 0, tzinfo=UTC)
CHECK_IN = datetime(2020, 8, 14, 20, 0, tzinfo=UTC)
CHECK_OUT = datetime(2020, 8, 15, 15, 0, tzinfo=UTC)
ADDRESS = "611 Historic Nature Trail Gatlinburg TN 37738 US"
SCRIPT = REPO_ROOT / "skills" / "drive-engine" / "answer_drive_or_fly.py"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    return tmp_path


def _lodging(role: str, when: datetime, hotel: str = "Fairfield Inn"):
    return {
        "type": "Lodging",
        "summary": f"{'Check-in:' if role == 'in' else 'Check-out:'} {hotel}",
        "start": when.isoformat().replace("+00:00", "Z"),
        "end": (when + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "location": ADDRESS,
    }


def _schedule(summary: str = "TN Tigers", start: str = "2020-08-14", end: str = "2020-08-16"):
    return [
        {"type": "Trip", "summary": summary, "start": start, "end": end, "location": "TN"},
        _lodging("in", CHECK_IN),
        _lodging("out", CHECK_OUT),
    ]


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", [VERDICT_DRIVE, VERDICT_FLY])
def test_an_answer_is_recorded_as_the_operators(answer):
    result = record_answer({"trip": "TN Tigers", "answer": answer}, schedule=_schedule(), now=NOW)
    assert result == {"recorded": True, "trip": "TN Tigers", "answer": answer}

    stored = load_verdicts(NOW)["tn-tigers-2020-08"]
    assert stored.verdict == answer
    assert stored.decided_by == DECIDED_BY_OPERATOR


@pytest.mark.parametrize("typed", ["Drive", "  FLY  ", "drive"])
def test_the_answer_is_matched_case_and_space_insensitively(typed):
    """The operator types the reply by hand."""
    result = record_answer({"trip": "TN Tigers", "answer": typed}, schedule=_schedule(), now=NOW)
    assert result["recorded"] is True


def test_the_trip_name_is_matched_case_and_space_insensitively():
    result = record_answer(
        {"trip": "  tn tigers ", "answer": VERDICT_DRIVE}, schedule=_schedule(), now=NOW
    )
    assert result["recorded"] is True


def test_the_recorded_answer_outlives_the_trip_itself():
    """It must still apply while the trip is under way, or the return leg would
    be re-decided from the band mid-trip."""
    record_answer({"trip": "TN Tigers", "answer": VERDICT_DRIVE}, schedule=_schedule(), now=NOW)
    assert load_verdicts(CHECK_OUT + timedelta(hours=12))


# ---------------------------------------------------------------------------
# Resolution failures are results, not crashes
# ---------------------------------------------------------------------------


def test_an_unknown_trip_reports_unmatched():
    result = record_answer(
        {"trip": "Some Other Trip", "answer": VERDICT_DRIVE}, schedule=_schedule(), now=NOW
    )
    assert result == {"recorded": False, "unmatched": "Some Other Trip"}
    assert load_verdicts(NOW) == {}


def test_two_same_named_trips_come_back_as_candidates():
    """Recording against the wrong one would silently mis-plan a trip."""
    schedule = _schedule() + [
        {
            "type": "Trip",
            "summary": "TN Tigers",
            "start": "2020-08-18",
            "end": "2020-08-19",
            "location": "TN",
        },
        _lodging("in", datetime(2020, 8, 18, 20, 0, tzinfo=UTC), hotel="Other Inn"),
        _lodging("out", datetime(2020, 8, 19, 15, 0, tzinfo=UTC), hotel="Other Inn"),
    ]
    result = record_answer(
        {"trip": "TN Tigers", "answer": VERDICT_DRIVE}, schedule=schedule, now=NOW
    )
    assert result["recorded"] is False
    assert len(result["candidates"]) == 2
    assert load_verdicts(NOW) == {}


@pytest.mark.parametrize("request_body", [{"answer": "drive"}, {"trip": "  ", "answer": "drive"}])
def test_a_missing_trip_name_is_a_caller_error(request_body):
    with pytest.raises(ValueError, match="trip"):
        record_answer(request_body, schedule=_schedule(), now=NOW)


@pytest.mark.parametrize("answer", ["maybe", "", None, "unknown"])
def test_only_drive_or_fly_are_accepted(answer):
    """`unknown` is a computed state, never something the operator answers."""
    with pytest.raises(ValueError, match="drive"):
        record_answer({"trip": "TN Tigers", "answer": answer}, schedule=_schedule(), now=NOW)


# ---------------------------------------------------------------------------
# CLI contract — stdout is always parseable JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["answer_drive_or_fly.py"], 2),
        (["answer_drive_or_fly.py", "not json"], 2),
        (["answer_drive_or_fly.py", '["a", "list"]'], 2),
        (["answer_drive_or_fly.py", '{"trip": "X", "answer": "maybe"}'], 2),
    ],
    ids=["no-arg", "bad-json", "not-an-object", "bad-answer"],
)
def test_usage_errors_exit_2_with_a_parseable_result(argv, code, capsys):
    assert main(argv) == code
    payload = json.loads(capsys.readouterr().out)
    assert payload["recorded"] is False
    assert payload["error"]


def test_invoked_as_a_subprocess_it_prints_json_not_a_traceback(tmp_path):
    """The skill parses stdout only; a traceback would read as "no result".
    No schedule exists at the container path here, which is exactly the
    degraded case that must still produce a result."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), json.dumps({"trip": "TN Tigers", "answer": "drive"})],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "DRIVE_PLANNER_STATE_DIR": str(tmp_path)},
        check=False,
    )
    payload = json.loads(proc.stdout)
    assert payload["recorded"] is False
    assert proc.returncode in (0, 1)
