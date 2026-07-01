"""Tests for skills/sync-tripit/precheck.py — the adaptive scheduler."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load sync-tripit's precheck.py under a distinct module name to avoid
# colliding with skills/flight-assist/precheck.py (which exists, has
# its own test suite, and gets imported as `precheck` by that suite).
# Two files cannot share `precheck` in sys.modules within a single pytest
# run, so loading by file path + unique module name gives each suite an
# independent module object.
_precheck_path = REPO_ROOT / "skills" / "sync-tripit" / "precheck.py"
_spec = importlib.util.spec_from_file_location("sync_tripit_precheck", _precheck_path)
assert _spec is not None and _spec.loader is not None, f"failed to locate {_precheck_path}"
precheck = importlib.util.module_from_spec(_spec)
sys.modules["sync_tripit_precheck"] = precheck
_spec.loader.exec_module(precheck)

# State writers + snapshot readers from the flight-assist skill. The
# precheck imports `state` lazily inside main() via `_load_flight_assist`;
# the tests use the same module to populate fixtures the precheck will
# later read via its own snapshot calls. `precheck` and this suite end
# up sharing one `state` module object via sys.modules.
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
import state as state_module  # noqa: E402
from state import (  # noqa: E402
    ACTIVE_FLIGHTS_FILE,
    STATE_SCHEMA_VERSION,
    write_active_flights,
    write_flight_state,
)

# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "state" / "flight-assist"
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(root))
    monkeypatch.setenv("BYAIR_MCP_URL", "https://api.byairapp.example/mcp?api_key=test")
    return root


def _flight_state(flight_id: int, *, dep_time: datetime) -> dict:
    """Build a minimally-valid flight state for the gate's scheduled_dep_time read."""
    return {
        "flight_id": flight_id,
        "code": "XX123",
        "ownership": "mine",
        "trip_id": 678,
        "scheduled_dep_time": dep_time.isoformat().replace("+00:00", "Z"),
        "scheduled_arr_time": (dep_time + timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
        "dep_airport_id": 20,
        "arr_airport_id": 28,
        "last_polled_at": dep_time.isoformat().replace("+00:00", "Z"),
        "last_snapshot": None,
        "phase_markers": {
            "day_before_fired": False,
            "time_to_leave_fired": False,
            "boarding_fired": False,
            "arrival_logistics_fired": False,
            "landed_acknowledged": False,
            "connection_at_risk_fired": False,
            "gate_assignment_fired": False,
        },
        "last_wake_at": None,
        "last_wake_reason": None,
    }


# --------------------------------------------------------------------
# _should_sync_now — the adaptive gate
# --------------------------------------------------------------------


def test_should_sync_cold_start_no_state(state_root):
    """No active-flights.json exists yet → first sync ever."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    should, reason = precheck._should_sync_now(state_module, now=now)
    assert should is True
    assert reason == "cold_start_no_state_file"


def test_should_sync_empty_state_recent_file(state_root):
    """State file exists but holds zero flights; mtime is fresh.
    Result: no imminent flights AND state is recent → skip the byAir call."""
    write_active_flights([])
    now = datetime.now(timezone.utc)
    should, reason = precheck._should_sync_now(state_module, now=now)
    assert should is False
    assert reason == "no_imminent_flights_recent_sync"


def test_should_sync_stale_state_triggers_sync(state_root):
    """State file mtime >6h ago → catch newly-booked trips between travel windows."""
    write_active_flights([])
    active_path = state_root / "active-flights.json"
    # Backdate mtime to 7 hours ago — past the 6h threshold.
    old_ts = time.time() - 7 * 3600
    os.utime(active_path, (old_ts, old_ts))
    now = datetime.now(timezone.utc)
    should, reason = precheck._should_sync_now(state_module, now=now)
    assert should is True
    assert reason.startswith("stale_state_age_")


def test_should_sync_imminent_flight_triggers_sync(state_root):
    """Any tracked flight with scheduled_dep_time in next 24h → poll for delays/gate changes."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    # Flight 4 hours from now — imminent.
    write_flight_state(_flight_state(101, dep_time=now + timedelta(hours=4)))
    write_active_flights([101])
    should, reason = precheck._should_sync_now(state_module, now=now)
    assert should is True
    assert reason == "imminent_flight_101"


def test_should_sync_flight_outside_24h_with_recent_state_skips(state_root):
    """Flight 48 hours away + recent state → both gates fail, skip."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_flight_state(_flight_state(202, dep_time=now + timedelta(hours=48)))
    write_active_flights([202])
    should, reason = precheck._should_sync_now(state_module, now=now)
    assert should is False
    assert reason == "no_imminent_flights_recent_sync"


def test_should_sync_flight_in_past_is_not_imminent(state_root):
    """Flight whose scheduled_dep_time has already passed shouldn't count as imminent.
    Edge case: a flight that departed an hour ago is in the past, not the future window."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_flight_state(_flight_state(303, dep_time=now - timedelta(hours=1)))
    write_active_flights([303])
    should, reason = precheck._should_sync_now(state_module, now=now)
    assert should is False
    assert reason == "no_imminent_flights_recent_sync"


def test_should_sync_one_imminent_among_many_triggers(state_root):
    """First-match-wins on the imminent gate. Two flights tracked: one in 48h, one in 6h."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_flight_state(_flight_state(404, dep_time=now + timedelta(hours=48)))
    write_flight_state(_flight_state(505, dep_time=now + timedelta(hours=6)))
    write_active_flights([404, 505])
    should, reason = precheck._should_sync_now(state_module, now=now)
    assert should is True
    # Reason names one specific flight — either is acceptable as long as
    # it's the imminent one (505), not the distant one (404).
    assert reason == "imminent_flight_505"


def test_should_sync_malformed_dep_time_is_skipped_not_crashed(state_root):
    """A flight state with malformed scheduled_dep_time should be skipped in the
    gate without crashing the precheck — the owner skill's writer validates the
    field, so a malformed value indicates a writer-side bug, not a gate concern."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    bad_state = _flight_state(606, dep_time=now + timedelta(hours=4))
    bad_state["scheduled_dep_time"] = "not-a-real-iso-string"
    write_flight_state(bad_state)
    write_active_flights([606])
    # No imminent flights (the malformed one is skipped) → fall through to
    # stale-state check → state is fresh → skip.
    should, reason = precheck._should_sync_now(state_module, now=now)
    assert should is False


def test_should_sync_does_not_migrate_old_active_flights(state_root):
    """Non-owner reader contract per coding-policy: stateful-artifacts.

    sync-tripit is a reader of flight-assist's state; it MUST NOT
    trigger schema migrations. An on-disk v1 active-flights.json must
    leave the file's bytes (and mtime) untouched after the precheck
    runs its gate.
    """
    state_root.mkdir(parents=True, exist_ok=True)
    active_path = state_root / ACTIVE_FLIGHTS_FILE
    # Write a v1-shape index by hand. The schema_version is lower than
    # the module's current STATE_SCHEMA_VERSION, so the owner-side
    # `read_active_flights` would migrate-and-rewrite. The snapshot
    # reader the precheck calls must not.
    legacy_payload = {"schema_version": STATE_SCHEMA_VERSION - 1, "flight_ids": [999]}
    active_path.write_text(json.dumps(legacy_payload))
    # Backdate mtime so a touch by the precheck would be detectable
    # against a freshly-stat'd "now" — gives the assert real signal.
    old_ts = time.time() - 60
    os.utime(active_path, (old_ts, old_ts))
    before_mtime = active_path.stat().st_mtime_ns
    before_bytes = active_path.read_bytes()

    now = datetime.now(timezone.utc)
    should, reason = precheck._should_sync_now(state_module, now=now)

    # Old schema is "no usable prior state" → falls through to the
    # mtime check, which sees a 60s-old file (well under the 6h stale
    # threshold) and returns no_imminent_flights_recent_sync. The
    # actual gate decision doesn't matter for this test; what matters
    # is that the file is unchanged.
    assert should is False
    assert reason == "no_imminent_flights_recent_sync"
    assert active_path.stat().st_mtime_ns == before_mtime
    assert active_path.read_bytes() == before_bytes


# --------------------------------------------------------------------
# main() — delegation to sync_tripit.py + outer-boundary contract
# --------------------------------------------------------------------


def _capture_stdout(monkeypatch):
    import io

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return buf


def test_main_emits_wake_false_on_no_sync_gate(state_root, monkeypatch):
    """When the gate fails, the precheck emits wake_agent=false directly without
    spawning the sync subprocess."""
    write_active_flights([])  # empty + fresh mtime → no_imminent_flights_recent_sync
    buf = _capture_stdout(monkeypatch)
    # Patch subprocess.run so a stray call would surface as a test failure.
    with patch("sync_tripit_precheck.subprocess.run") as mock_run:
        rc = precheck.main()
    assert rc == 0
    assert mock_run.called is False
    payload = json.loads(buf.getvalue().strip())
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "no_imminent_flights_recent_sync"


def test_main_delegates_to_sync_tripit_on_gate_pass(state_root, monkeypatch):
    """When the gate passes, the precheck spawns sync_tripit.py and forwards
    its stdout verbatim."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_flight_state(_flight_state(707, dep_time=now + timedelta(hours=2)))
    write_active_flights([707])

    sync_output = json.dumps({"wake_agent": True, "data": {"events": [{"flight_id": 707}]}})

    class _FakeResult:
        stdout = sync_output + "\n"
        stderr = ""

    buf = _capture_stdout(monkeypatch)
    with patch("sync_tripit_precheck.datetime") as mock_datetime:
        mock_datetime.now.return_value = now
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.fromtimestamp = datetime.fromtimestamp
        with patch("sync_tripit_precheck.subprocess.run", return_value=_FakeResult()) as mock_run:
            rc = precheck.main()

    assert rc == 0
    mock_run.assert_called_once()
    # Sync output appears on stdout (precheck doesn't reshape it).
    assert sync_output in buf.getvalue()


def test_main_handles_empty_sync_subprocess_stdout(state_root, monkeypatch):
    """If sync_tripit.py crashed and produced no stdout, the precheck must
    emit a safe-shape payload so the cadence-registry isn't fed invalid JSON."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_flight_state(_flight_state(808, dep_time=now + timedelta(hours=2)))
    write_active_flights([808])

    class _EmptyResult:
        stdout = ""
        stderr = "tracebacks went here\n"

    buf = _capture_stdout(monkeypatch)
    with patch("sync_tripit_precheck.datetime") as mock_datetime:
        mock_datetime.now.return_value = now
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.fromtimestamp = datetime.fromtimestamp
        with patch("sync_tripit_precheck.subprocess.run", return_value=_EmptyResult()):
            rc = precheck.main()

    assert rc == 0
    payload = json.loads(buf.getvalue().strip())
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "sync_no_output"


def test_main_handles_subprocess_timeout(state_root, monkeypatch):
    """A hung sync_tripit subprocess that breaches the 60s budget must be
    converted to a safe-shape wake_agent=false payload, not an unhandled
    exception that the agent-runner reads as 'skip wake'."""
    import subprocess as _subprocess

    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    write_flight_state(_flight_state(909, dep_time=now + timedelta(hours=2)))
    write_active_flights([909])

    buf = _capture_stdout(monkeypatch)
    with patch("sync_tripit_precheck.datetime") as mock_datetime:
        mock_datetime.now.return_value = now
        mock_datetime.fromisoformat = datetime.fromisoformat
        mock_datetime.fromtimestamp = datetime.fromtimestamp
        with patch(
            "sync_tripit_precheck.subprocess.run",
            side_effect=_subprocess.TimeoutExpired(cmd="x", timeout=60),
        ):
            rc = precheck.main()

    assert rc == 0
    payload = json.loads(buf.getvalue().strip())
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "sync_subprocess_timeout"


def test_main_outer_boundary_catches_unexpected_exception(state_root, monkeypatch):
    """Any unexpected exception in the gate or delegation must be caught
    and converted to a safe-shape wake_agent=false payload + exit 0.
    The agent-runner contract reads non-zero exit OR invalid JSON as
    'skip wake', so an unhandled exception would silently disable polling."""
    buf = _capture_stdout(monkeypatch)
    with patch(
        "sync_tripit_precheck._should_sync_now",
        side_effect=RuntimeError("synthetic gate failure"),
    ):
        rc = precheck.main()
    assert rc == 0
    payload = json.loads(buf.getvalue().strip())
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "precheck_internal_error"


def test_main_bootstrap_failure_emits_safe_json(monkeypatch):
    """If `_load_flight_assist` raises (e.g. the co-shipped skill is
    missing from the install), the outer-boundary handler in main()
    must still emit the safe-shape JSON and exit 0. Without this path,
    a bootstrap FileNotFoundError would surface as exit 1 + empty
    stdout, which the agent-runner reads as wake_agent=false silently
    — exactly the failure mode the outer-boundary-process-contract
    carve-out exists to prevent.

    Patching `_load_flight_assist` is the structural replacement for
    the previous module-level catch-all (now removed per OpenAI policy
    reviewer feedback on PR #21 — handlers must sit at the outermost
    process boundary, not at module-load level).
    """
    buf = _capture_stdout(monkeypatch)

    def _boom():
        raise FileNotFoundError("synthetic bootstrap failure")

    monkeypatch.setattr(precheck, "_load_flight_assist", _boom)
    rc = precheck.main()
    assert rc == 0
    payload = json.loads(buf.getvalue().strip())
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "precheck_internal_error"
