"""Tests for the drive-planner recheck poll core (`drive-planner-recheck/precheck.py`).

Exercises `evaluate_blocks` with an injected router (no live maps, no live
calendar) over fetched-event fixtures built from the real block codec, so the
poll reads exactly the blocks the sweep writes. Covers: the traffic-growth
alert and its one-shot suppression, the independent leave-by alert, return-leg
and not-yet-due skips, and route-error recording (no silent miss).

The module is loaded under a unique name — its bare module name `precheck` is
shared with flight-assist's and drive-planner's.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVE = REPO_ROOT / "skills" / "drive-planner"
RECHECK = REPO_ROOT / "skills" / "drive-planner-recheck"
sys.path.insert(0, str(DRIVE))
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))  # maps_client for _route_seconds

from block_props import (  # noqa: E402
    ALERT_GROWTH,
    ALERT_LEAVE_NOW,
    build_description,
    parse_alerted,
)
from route_error import RouteError  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


poll = _load("drive_planner_recheck_precheck", RECHECK / "precheck.py")

UTC = timezone.utc
NOW = datetime(2026, 7, 2, 16, 0, tzinfo=UTC)
BASELINE = 1500  # 25 min
BUFFER = 300  # 5 min default folded into leave-by


def _block_event(
    *,
    direction="outbound",
    arrive_offset_min,
    alerted=None,
    event_id="block_1",
    meeting_id="evt_42",
):
    """A fetched drive-block event with arrive_by = NOW + arrive_offset_min."""
    arrive = NOW + timedelta(minutes=arrive_offset_min)
    summary = f"Drive: {meeting_id}"
    description = build_description(
        summary=summary,
        meeting_id=meeting_id,
        direction=direction,
        baseline_seconds=BASELINE,
        arrive_by=arrive,
        origin="Home",
        destination="100 Broadway, Nashville, TN",
        alerted=parse_alerted(alerted) if alerted is not None else frozenset(),
    )
    return {"id": event_id, "summary": summary, "description": description}


def _route(seconds):
    return lambda origin, destination: seconds


# --- traffic-growth alert + suppression ----------------------------------


def test_growth_past_threshold_alerts_once():
    # arrive in 60 min → leave_by = NOW+30m, due now; +12 min of traffic > 10m
    # threshold but leave-by still ~18 min out (no leave-now).
    event = _block_event(arrive_offset_min=60)
    result = poll.evaluate_blocks([event], now=NOW, route=_route(BASELINE + 720))
    assert len(result["alerts"]) == 1
    assert result["alerts"][0]["kinds"] == [ALERT_GROWTH]
    # display-ready minutes the SKILL.md consumes verbatim (no ÷60 in prose)
    assert result["alerts"][0]["current_minutes"] == round((BASELINE + 720) / 60)
    assert result["alerts"][0]["delta_minutes"] == round(720 / 60)
    # The patch carries the full rebuilt description (state lives there); the
    # alert record is updated and the rest of the state preserved.
    patch = result["patches"][0]
    assert f'"al":"{ALERT_GROWTH}"' in patch["description"]
    assert "[drive-planner:meeting=evt_42:dir=outbound]" in patch["description"]
    assert f'"b":{BASELINE}' in patch["description"]
    assert result["route_errors"] == []


def test_growth_already_alerted_is_suppressed():
    event = _block_event(arrive_offset_min=60, alerted=ALERT_GROWTH)
    result = poll.evaluate_blocks([event], now=NOW, route=_route(BASELINE + 720))
    assert result["alerts"] == []
    assert result["patches"] == []


def test_no_growth_no_alert():
    event = _block_event(arrive_offset_min=60)
    result = poll.evaluate_blocks([event], now=NOW, route=_route(BASELINE + 60))
    assert result["alerts"] == []


# --- leave-by alert (independent of growth) ------------------------------


def test_leave_by_passed_alerts():
    # arrive in 20 min, baseline drive 25 min → leave-by already past; even with
    # no traffic growth the user must leave now.
    event = _block_event(arrive_offset_min=20)
    result = poll.evaluate_blocks([event], now=NOW, route=_route(BASELINE))
    assert len(result["alerts"]) == 1
    assert result["alerts"][0]["kinds"] == [ALERT_LEAVE_NOW]


def test_leave_now_fires_even_after_prior_growth_alert():
    event = _block_event(arrive_offset_min=20, alerted=ALERT_GROWTH)
    result = poll.evaluate_blocks([event], now=NOW, route=_route(BASELINE))
    assert result["alerts"][0]["kinds"] == [ALERT_LEAVE_NOW]
    # both prior growth and the new leave_now are in the carried-forward record
    desc = result["patches"][0]["description"]
    assert ALERT_GROWTH in desc and ALERT_LEAVE_NOW in desc


# --- skips ---------------------------------------------------------------


def test_return_leg_is_never_rechecked():
    event = _block_event(direction="return", arrive_offset_min=60)
    result = poll.evaluate_blocks([event], now=NOW, route=_route(BASELINE + 720))
    assert result["alerts"] == []


def test_block_not_yet_due_is_skipped():
    # arrive in 6 hours → leave-by far in the future, outside the 45-min window.
    event = _block_event(arrive_offset_min=360)
    result = poll.evaluate_blocks([event], now=NOW, route=_route(BASELINE + 720))
    assert result["alerts"] == []


def test_alert_summary_falls_back_to_meeting_id():
    # A fetched block without a `summary` must not produce "Leave now for None".
    event = _block_event(arrive_offset_min=20)
    del event["summary"]
    result = poll.evaluate_blocks([event], now=NOW, route=_route(BASELINE))
    assert result["alerts"][0]["summary"] == "evt_42"


def test_non_block_event_ignored():
    plain = {"id": "m", "summary": "Customer sync", "description": "no marker"}
    result = poll.evaluate_blocks([plain], now=NOW, route=_route(BASELINE + 720))
    assert result["alerts"] == []


# --- no silent miss ------------------------------------------------------


def test_route_seconds_translates_read_timeout_to_route_error():
    class TimingOutMaps:
        def travel_time(self, *, origin, destination):
            raise TimeoutError("read timed out")

    import pytest

    with pytest.raises(RouteError):
        poll._route_seconds(TimingOutMaps(), "a", "b")


def test_route_failure_recorded_not_alerted():
    def boom(origin, destination):
        raise RouteError("ALL_PROVIDERS_FAILED")

    event = _block_event(arrive_offset_min=60)
    result = poll.evaluate_blocks([event], now=NOW, route=boom)
    assert result["alerts"] == []
    assert len(result["route_errors"]) == 1
    assert "ALL_PROVIDERS_FAILED" in result["route_errors"][0]["error"]
    # A route error with no alert must still wake the agent so the outage is
    # surfaced (not silently dropped).
    assert poll.should_wake(result) is True


def test_should_wake_only_when_alerts_or_route_errors():
    assert poll.should_wake({"alerts": [], "route_errors": []}) is False
    assert poll.should_wake({"alerts": [{"x": 1}], "route_errors": []}) is True
    assert poll.should_wake({"alerts": [], "route_errors": [{"e": 1}]}) is True


# --- outer-boundary JSON contract -----------------------------------------


def test_missing_sibling_bundle_emits_no_wake_payload(monkeypatch, capsys):
    """A missing / mis-mounted co-shipped drive-planner bundle must surface
    through the outer-boundary JSON contract — `{"wake_agent": false, ...}`
    on stdout with exit 0 — never a raw crash before `main()` enters its
    try block (#126). The resolver is patched to raise the same
    FileNotFoundError a missing skill mount produces."""
    import json

    def _raise(runtime, dev, what):
        raise FileNotFoundError(f"cannot locate the co-shipped {what} skill")

    monkeypatch.setattr(poll, "_resolve", _raise)
    code = poll.main()
    out, err = capsys.readouterr()
    assert code == 0
    payload = json.loads(out)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "recheck_precheck_internal_error"
    # The fault is still diagnosable: the traceback lands on stderr.
    assert "FileNotFoundError" in err
