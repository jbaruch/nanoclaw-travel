"""Tests for the drive-planner recheck gate (`recheck.py`).

The gate decides whether a scheduled T-45 / T-30 / T-15 recheck should ping
the user. Tests pin the two alert triggers (growth past threshold, leave-by
arrived), the silence cases, the leave-by recompute math, the input guards,
and the precheck-gating CLI contract. All fixtures are fixed values — no
live routing, no random data.
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-planner"))

from recheck import (  # noqa: E402
    DEFAULT_ALERT_THRESHOLD_SECONDS,
    DEFAULT_ARRIVAL_BUFFER_SECONDS,
    RecheckDecision,
    RecheckError,
    evaluate_recheck,
    main,
)

CT = timezone(timedelta(hours=-5))
NOW = datetime(2026, 7, 1, 8, 0, tzinfo=CT)
# Meeting at 09:00; with a 25-min baseline drive and 5-min buffer the leave-by
# sits at 08:30, half an hour out from NOW.
ARRIVE_BY = datetime(2026, 7, 1, 9, 0, tzinfo=CT)
BASELINE = 25 * 60


# --- alert triggers and silence ------------------------------------------


def test_below_threshold_with_time_to_spare_is_silent():
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=BASELINE + 4 * 60,  # +4 min, under the 10-min threshold
        arrive_by=ARRIVE_BY,
        now=NOW,
    )
    assert decision.alert is False
    assert decision.grew_past_threshold is False
    assert decision.leave_by_passed is False
    assert decision.reason == "no significant change"


def test_growth_past_threshold_alerts():
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=BASELINE + 12 * 60,  # +12 min
        arrive_by=ARRIVE_BY,
        now=NOW,
    )
    assert decision.alert is True
    assert decision.grew_past_threshold is True
    assert decision.delta_seconds == 12 * 60
    assert decision.reason == "traffic grew past threshold"


def test_growth_exactly_at_threshold_alerts():
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=BASELINE + DEFAULT_ALERT_THRESHOLD_SECONDS,
        arrive_by=ARRIVE_BY,
        now=NOW,
    )
    assert decision.grew_past_threshold is True  # boundary is inclusive (>=)


def test_traffic_improved_is_silent():
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=BASELINE - 5 * 60,  # got faster
        arrive_by=ARRIVE_BY,
        now=NOW,
    )
    assert decision.alert is False
    assert decision.delta_seconds == -5 * 60


def test_leave_by_passed_alerts_even_without_growth():
    # Recheck fires late: now is 08:45, the (unchanged) 25-min drive needs an
    # 08:30 leave-by — already 15 min gone. Alert despite zero growth.
    late_now = datetime(2026, 7, 1, 8, 45, tzinfo=CT)
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=BASELINE,
        arrive_by=ARRIVE_BY,
        now=late_now,
    )
    assert decision.alert is True
    assert decision.leave_by_passed is True
    assert decision.grew_past_threshold is False
    assert decision.seconds_until_leave_by < 0
    assert decision.reason == "leave-by has arrived"


def test_both_triggers_compose_in_reason():
    late_now = datetime(2026, 7, 1, 8, 50, tzinfo=CT)
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=BASELINE + 15 * 60,
        arrive_by=ARRIVE_BY,
        now=late_now,
    )
    assert decision.alert is True
    assert decision.reason == "traffic grew past threshold and the leave-by has arrived"


# --- leave-by recompute math ---------------------------------------------


def test_new_leave_by_subtracts_drive_and_buffer():
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=30 * 60,
        arrive_by=ARRIVE_BY,
        now=NOW,
        buffer_seconds=5 * 60,
    )
    # 09:00 − 30 min drive − 5 min buffer = 08:25
    assert decision.new_leave_by == datetime(2026, 7, 1, 8, 25, tzinfo=CT)
    assert decision.seconds_until_leave_by == 25 * 60


def test_default_buffer_is_applied_when_omitted():
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=20 * 60,
        arrive_by=ARRIVE_BY,
        now=NOW,
    )
    # 09:00 − 20 min − default 5 min buffer = 08:35
    expected = ARRIVE_BY - timedelta(seconds=20 * 60 + DEFAULT_ARRIVAL_BUFFER_SECONDS)
    assert decision.new_leave_by == expected


def test_custom_threshold_changes_decision():
    decision = evaluate_recheck(
        baseline_seconds=BASELINE,
        current_seconds=BASELINE + 6 * 60,  # +6 min
        arrive_by=ARRIVE_BY,
        now=NOW,
        threshold_seconds=5 * 60,  # lower bar → now it alerts
    )
    assert decision.grew_past_threshold is True


# --- input guards --------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 1.5, "600", True, None])
def test_negative_or_non_int_duration_raises(bad):
    with pytest.raises(RecheckError, match="current_seconds"):
        evaluate_recheck(
            baseline_seconds=BASELINE,
            current_seconds=bad,  # type: ignore[arg-type]
            arrive_by=ARRIVE_BY,
            now=NOW,
        )


def test_naive_arrive_by_raises():
    with pytest.raises(RecheckError, match="arrive_by"):
        evaluate_recheck(
            baseline_seconds=BASELINE,
            current_seconds=BASELINE,
            arrive_by=datetime(2026, 7, 1, 9, 0),  # naive
            now=NOW,
        )


def test_naive_now_raises():
    with pytest.raises(RecheckError, match="now"):
        evaluate_recheck(
            baseline_seconds=BASELINE,
            current_seconds=BASELINE,
            arrive_by=ARRIVE_BY,
            now=datetime(2026, 7, 1, 8, 0),  # naive
        )


def test_decision_is_frozen():
    decision = evaluate_recheck(
        baseline_seconds=BASELINE, current_seconds=BASELINE, arrive_by=ARRIVE_BY, now=NOW
    )
    with pytest.raises(FrozenInstanceError):
        decision.alert = True  # type: ignore[misc]


# --- CLI precheck-gating contract ----------------------------------------


class _FakeStdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


def _run_cli(monkeypatch, capsys, request) -> tuple[int, str, str]:
    stdin_text = request if isinstance(request, str) else json.dumps(request)
    monkeypatch.setattr("sys.stdin", _FakeStdin(stdin_text))
    monkeypatch.setattr("sys.argv", ["recheck.py"])
    code = main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_alert_sets_wake_agent_true(monkeypatch, capsys):
    request = {
        "baseline_seconds": BASELINE,
        "current_seconds": BASELINE + 15 * 60,
        "arrive_by": ARRIVE_BY.isoformat(),
        "now": NOW.isoformat(),
    }
    code, out, err = _run_cli(monkeypatch, capsys, request)
    assert code == 0
    assert err == ""
    payload = json.loads(out)
    assert payload["wake_agent"] is True
    assert payload["data"]["grew_past_threshold"] is True
    assert payload["data"]["new_leave_by"].startswith("2026-07-01")


def test_cli_no_alert_sets_wake_agent_false(monkeypatch, capsys):
    request = {
        "baseline_seconds": BASELINE,
        "current_seconds": BASELINE + 60,
        "arrive_by": ARRIVE_BY.isoformat(),
        "now": NOW.isoformat(),
    }
    code, out, _err = _run_cli(monkeypatch, capsys, request)
    assert code == 0
    assert json.loads(out)["wake_agent"] is False


def test_cli_optional_buffer_and_threshold_honored(monkeypatch, capsys):
    request = {
        "baseline_seconds": BASELINE,
        "current_seconds": BASELINE + 6 * 60,
        "arrive_by": ARRIVE_BY.isoformat(),
        "now": NOW.isoformat(),
        "threshold_seconds": 5 * 60,
    }
    code, out, _err = _run_cli(monkeypatch, capsys, request)
    assert code == 0
    assert json.loads(out)["wake_agent"] is True


def test_cli_invalid_json_exits_nonzero(monkeypatch, capsys):
    code, out, err = _run_cli(monkeypatch, capsys, "{nope")
    assert code == 1
    assert out == ""
    assert json.loads(err)["error"].startswith("invalid JSON")


def test_cli_non_object_root_exits_nonzero(monkeypatch, capsys):
    code, _out, err = _run_cli(monkeypatch, capsys, [1, 2])
    assert code == 1
    assert "JSON object" in json.loads(err)["error"]


def test_cli_naive_now_exits_nonzero(monkeypatch, capsys):
    request = {
        "baseline_seconds": BASELINE,
        "current_seconds": BASELINE,
        "arrive_by": ARRIVE_BY.isoformat(),
        "now": "2026-07-01T08:00:00",  # naive
    }
    code, _out, err = _run_cli(monkeypatch, capsys, request)
    assert code == 1
    assert "now" in json.loads(err)["error"]


@pytest.mark.parametrize("bad", ["xyz", -5, 1.5, True])
def test_cli_bad_duration_exits_nonzero(monkeypatch, capsys, bad):
    request = {
        "baseline_seconds": bad,
        "current_seconds": BASELINE,
        "arrive_by": ARRIVE_BY.isoformat(),
        "now": NOW.isoformat(),
    }
    code, _out, err = _run_cli(monkeypatch, capsys, request)
    assert code == 1
    assert "baseline_seconds" in json.loads(err)["error"]


def test_decision_dataclass_exposed():
    assert RecheckDecision.__dataclass_fields__["alert"] is not None
