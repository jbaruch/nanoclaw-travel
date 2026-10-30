"""Tests for skills/flight-assist/sync_tripit.py."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from helpers import must

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

import sync_tripit  # noqa: E402
from state import (  # noqa: E402
    read_active_flights,
    read_flight_state,
    read_sync_health,
    write_active_flights,
    write_flight_state,
)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "state" / "flight-assist"
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(root))
    monkeypatch.setenv("BYAIR_MCP_URL", "https://api.byairapp.example/mcp?api_key=test")
    return root


def _trips_payload(flights: list[dict]) -> dict:
    """Wrap a list of flight dicts in a single-trip byair_list_trips response."""
    return {
        "trips": [
            {
                "id": 678,
                "name": "Test Trip",
                "flights": flights,
            }
        ]
    }


def _flight(flight_id: int, code: str = "XX123") -> dict:
    return {
        "id": flight_id,
        "code": code,
        "ownership": "mine",
        "scheduledDepTime": "2026-05-18T17:00:00+00:00",
        "scheduledArrTime": "2026-05-18T20:00:00+00:00",
        "depAirport": {"id": 20, "name": "San Francisco International Airport"},
        "arrAirport": {"id": 28, "name": "Phoenix"},
    }


# ---------------------------------------------------------------------------
# Reconcile semantics
# ---------------------------------------------------------------------------


def test_first_sync_adds_all_upstream_flights(state_root: Path):
    payload = _trips_payload([_flight(100), _flight(200)])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert [e["flight_id"] for e in diff["added"]] == [100, 200]
    assert diff["removed"] == []
    assert read_active_flights() == [100, 200]
    assert read_flight_state(100) is not None
    assert read_flight_state(200) is not None


def test_sync_fetches_only_owned_trips(state_root: Path):
    """#29: the sync must request ownership="mine" so friends' tracked
    flights never enter the active-flights index and never surface as
    [M] wake events. byAir's list_trips default is "all"."""
    payload = _trips_payload([_flight(100)])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        sync_tripit._run_sync(now_utc=fake_now)
    mock_byair.return_value.list_trips.assert_called_once_with(status="active", ownership="mine")


def test_sync_bounds_the_byair_client_below_the_precheck_budget(state_root: Path):
    """#212: the client must be constructed with the bounded per-call
    timeout, not left on `ByAirClient`'s 30s default.

    This script runs as a subprocess inside sync-tripit's precheck budget.
    A call left at the default spends a third of that budget on one hung
    upstream and rides the outer subprocess timeout — losing the diff and
    emitting a bare `sync_subprocess_timeout` — instead of failing fast
    into the `URLError` transient branch that skips the pass and lets the
    next 5-min fire retry.

    Asserted at the construction site, not just as a constant: the
    ordering checks in `test_sync_tripit_precheck_budget.py` all still
    pass if the `timeout=` argument is dropped here.
    """
    payload = _trips_payload([_flight(100)])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        sync_tripit._run_sync(now_utc=fake_now)
    mock_byair.assert_called_once_with(timeout=sync_tripit._BYAIR_CALL_TIMEOUT_SECONDS)


def test_sync_with_unchanged_upstream_no_diff(state_root: Path):
    write_active_flights([100])
    sync_tripit.initialize_flight_from_byair(
        flight=_flight(100), now_utc=datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    )
    payload = _trips_payload([_flight(100)])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=datetime(2026, 5, 18, 4, 5, 0, tzinfo=UTC))
    assert diff["added"] == []
    assert diff["removed"] == []


def test_sync_repairs_poisoned_marketing_code_on_retained_flight(state_root: Path):
    """#159: a codeshare whose `code` was overwritten with the operating
    designator (9E4908) by a prior poll is healed from list_trips' marketing
    designator (DL4908) on the next sync — on both the record and the snapshot
    mirror. Preservation alone would keep the corrupted value; nothing else
    heals it."""
    write_active_flights([100])
    sync_tripit.initialize_flight_from_byair(
        flight=_flight(100, code="9E4908"),
        now_utc=datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC),
    )
    poisoned = must(read_flight_state(100))
    poisoned["last_snapshot"] = {"code": "9E4908", "computed_status": "scheduled"}
    write_flight_state(poisoned)

    payload = _trips_payload([_flight(100, code="DL4908")])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=datetime(2026, 5, 18, 4, 5, 0, tzinfo=UTC))

    assert [e["flight_id"] for e in diff["repaired"]] == [100]
    healed = must(read_flight_state(100))
    assert healed["code"] == "DL4908"
    assert healed["last_snapshot"]["code"] == "DL4908"


def test_sync_does_not_repair_when_code_already_marketing(state_root: Path):
    """A retained flight whose stored code already matches upstream is left
    untouched — no needless rewrite, no repair entry."""
    write_active_flights([100])
    sync_tripit.initialize_flight_from_byair(
        flight=_flight(100, code="DL4908"),
        now_utc=datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC),
    )
    payload = _trips_payload([_flight(100, code="DL4908")])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=datetime(2026, 5, 18, 4, 5, 0, tzinfo=UTC))

    assert diff["repaired"] == []
    assert must(read_flight_state(100))["code"] == "DL4908"


def test_sync_removes_expired_flights(state_root: Path):
    # Initial state: tracking 100 and 200
    write_active_flights([100, 200])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    sync_tripit.initialize_flight_from_byair(flight=_flight(100), now_utc=fake_now)
    sync_tripit.initialize_flight_from_byair(flight=_flight(200), now_utc=fake_now)
    # Upstream now only reports 100
    payload = _trips_payload([_flight(100)])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert diff["added"] == []
    assert [e["flight_id"] for e in diff["removed"]] == [200]
    # The removed-event payload carries the prior-state metadata so SKILL.md
    # can compose the notification without re-reading the (now-deleted) state.
    assert diff["removed"][0]["code"] == "XX123"
    assert diff["removed"][0]["scheduled_dep_time"] == "2026-05-18T17:00:00+00:00"
    assert read_active_flights() == [100]
    assert read_flight_state(200) is None
    assert read_flight_state(100) is not None


def _ledger_entry() -> dict:
    return {
        "boarding": {
            "event_id": "evt_boarding",
            "calendar_id": "c_byair@group.calendar.google.com",
            "managed": "created",
            "synced_signature": "s",
        }
    }


def test_sync_retains_removed_flight_with_calendar_ledger_as_tombstone(state_root: Path):
    """#55: a flight that drops from upstream but still has managed calendar
    events is kept on disk as a teardown tombstone, NOT deleted — the
    reconcile sweep needs the ledger to delete those events. It still leaves
    active-flights and still surfaces in the removed-event payload."""
    write_active_flights([100, 200])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    sync_tripit.initialize_flight_from_byair(flight=_flight(100), now_utc=fake_now)
    sync_tripit.initialize_flight_from_byair(flight=_flight(200), now_utc=fake_now)
    # Flight 200 has carried a calendar ledger forward.
    state_200 = must(read_flight_state(200))
    state_200["calendar_events"] = _ledger_entry()
    write_flight_state(state_200)

    payload = _trips_payload([_flight(100)])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)

    assert [e["flight_id"] for e in diff["removed"]] == [200]
    assert read_active_flights() == [100]
    # Retained as a tombstone — the ledger survives for the reconcile sweep.
    retained = read_flight_state(200)
    assert retained is not None
    assert retained["calendar_events"] == _ledger_entry()


def test_sync_deletes_removed_flight_without_ledger(state_root: Path):
    """#55: the tombstone retention is scoped to flights with a non-empty
    ledger. A removed flight with nothing to tear down is deleted immediately,
    preserving the prior behavior."""
    write_active_flights([100, 200])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    sync_tripit.initialize_flight_from_byair(flight=_flight(100), now_utc=fake_now)
    sync_tripit.initialize_flight_from_byair(flight=_flight(200), now_utc=fake_now)

    payload = _trips_payload([_flight(100)])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        sync_tripit._run_sync(now_utc=fake_now)

    assert read_flight_state(200) is None


def test_sync_adds_and_removes_in_one_pass(state_root: Path):
    write_active_flights([100])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    sync_tripit.initialize_flight_from_byair(flight=_flight(100), now_utc=fake_now)
    payload = _trips_payload([_flight(200), _flight(300)])
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert [e["flight_id"] for e in diff["added"]] == [200, 300]
    assert [e["flight_id"] for e in diff["removed"]] == [100]
    assert read_active_flights() == [200, 300]


def test_sync_handles_empty_upstream(state_root: Path):
    write_active_flights([100, 200])
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    sync_tripit.initialize_flight_from_byair(flight=_flight(100), now_utc=fake_now)
    sync_tripit.initialize_flight_from_byair(flight=_flight(200), now_utc=fake_now)
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = {"trips": []}
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert [e["flight_id"] for e in diff["removed"]] == [100, 200]
    assert read_active_flights() == []


def test_initialize_flight_with_flight_id_key(state_root: Path):
    """initialize_flight_from_byair tolerates `flight_id` instead of `id`."""
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    flight = {
        "flight_id": 42,  # precheck's internal key, not byair's `id`
        "code": "XX42",
        "ownership": "mine",
        "scheduledDepTime": "2026-05-18T17:00:00+00:00",
        "scheduledArrTime": "2026-05-18T20:00:00+00:00",
        "depAirport": {"id": 20},
        "arrAirport": {"id": 28},
    }
    sync_tripit.initialize_flight_from_byair(flight=flight, now_utc=fake_now)
    state = read_flight_state(42)
    assert state is not None
    assert state["flight_id"] == 42
    assert state["code"] == "XX42"


def test_initialize_flight_with_id_key(state_root: Path):
    """initialize_flight_from_byair also tolerates `id` (byair raw shape)."""
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    sync_tripit.initialize_flight_from_byair(flight=_flight(42), now_utc=fake_now)
    state = read_flight_state(42)
    assert state is not None
    assert state["flight_id"] == 42


def test_initialize_flight_with_no_id_is_a_no_op(state_root: Path):
    """A flight dict without either id or flight_id silently no-ops."""
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    sync_tripit.initialize_flight_from_byair(flight={"code": "XX"}, now_utc=fake_now)
    assert read_active_flights() == []  # nothing written


def test_sync_handles_multi_trip_payload(state_root: Path):
    """Each trip's flights all roll into the same active-flights index."""
    fake_now = datetime(2026, 5, 18, 4, 0, 0, tzinfo=UTC)
    payload = {
        "trips": [
            {"id": 1, "name": "T1", "flights": [_flight(100)]},
            {"id": 2, "name": "T2", "flights": [_flight(200), _flight(300)]},
        ]
    }
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        mock_byair.return_value.list_trips.return_value = payload
        diff = sync_tripit._run_sync(now_utc=fake_now)
    assert [e["flight_id"] for e in diff["added"]] == [100, 200, 300]
    assert must(read_flight_state(100))["trip_id"] == 1
    assert must(read_flight_state(200))["trip_id"] == 2
    assert must(read_flight_state(300))["trip_id"] == 2


# ---------------------------------------------------------------------------
# byAir health counter (#326)
# ---------------------------------------------------------------------------


def _main_payload(capsys, *, list_trips_side_effect=None, list_trips_return=None) -> dict:
    with patch("sync_tripit.ByAirClient.from_env") as mock_byair:
        if list_trips_side_effect is not None:
            mock_byair.return_value.list_trips.side_effect = list_trips_side_effect
        else:
            mock_byair.return_value.list_trips.return_value = list_trips_return
        assert sync_tripit.main() == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _source_events(payload: dict) -> list[dict]:
    return [e["event"] for e in payload["data"]["events"] if e["flight_id"] is None]


def test_outage_alert_fires_once_at_threshold(state_root: Path, capsys):
    """Failed passes stay quiet until the threshold, alert exactly once, and
    do not re-alert while the same failure run continues."""
    failure = sync_tripit.ByAirError("session_missing", "no mcp-session-id")
    payloads = [
        _main_payload(capsys, list_trips_side_effect=failure)
        for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD + 1)
    ]
    for quiet in payloads[: sync_tripit._OUTAGE_ALERT_THRESHOLD - 1]:
        assert quiet["wake_agent"] is False
        assert _source_events(quiet) == []
    alert = payloads[sync_tripit._OUTAGE_ALERT_THRESHOLD - 1]
    assert alert["wake_agent"] is True
    assert _source_events(alert) == [
        {
            "reason": "byair_unreachable",
            "consecutive_failures": sync_tripit._OUTAGE_ALERT_THRESHOLD,
            "error": "session_missing",
        }
    ]
    assert payloads[-1]["wake_agent"] is False
    assert _source_events(payloads[-1]) == []


def test_an_alert_that_never_reached_stdout_is_sent_again(state_root: Path, capsys):
    """#327 review: `alerted` means the alert went out. A pass whose emission
    fails must leave the health record untouched, so the next pass re-sends."""
    failure = sync_tripit.ByAirError("session_missing", "no mcp-session-id")
    for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD - 1):
        _main_payload(capsys, list_trips_side_effect=failure)
    before = read_sync_health()
    with (
        patch("sync_tripit.ByAirClient.from_env") as mock_byair,
        patch("sync_tripit._emit_diff", side_effect=BrokenPipeError("stdout closed")),
    ):
        mock_byair.return_value.list_trips.side_effect = failure
        with pytest.raises(BrokenPipeError):
            sync_tripit.main()
    capsys.readouterr()
    assert read_sync_health() == before
    again = _main_payload(capsys, list_trips_side_effect=failure)
    assert [e["reason"] for e in _source_events(again)] == ["byair_unreachable"]


def test_recovery_after_alert_emits_recovered_and_resets(state_root: Path, capsys):
    failure = sync_tripit.ByAirError("session_missing", "no mcp-session-id")
    for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD):
        _main_payload(capsys, list_trips_side_effect=failure)
    recovered = _main_payload(capsys, list_trips_return={"trips": []})
    assert recovered["wake_agent"] is True
    assert _source_events(recovered) == [
        {
            "reason": "byair_recovered",
            "failures_before_recovery": sync_tripit._OUTAGE_ALERT_THRESHOLD,
        }
    ]
    # The run is over: the next failures start counting from zero again.
    after = _main_payload(capsys, list_trips_side_effect=failure)
    assert _source_events(after) == []


def test_clean_pass_below_threshold_resets_silently(state_root: Path, capsys):
    failure = urllib.error.URLError("synthetic network failure")
    for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD - 1):
        _main_payload(capsys, list_trips_side_effect=failure)
    clean = _main_payload(capsys, list_trips_return={"trips": []})
    assert _source_events(clean) == []
    for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD - 1):
        quiet = _main_payload(capsys, list_trips_side_effect=failure)
        assert _source_events(quiet) == []


def _internal_failure_payload(capsys) -> dict:
    with patch("sync_tripit.ByAirClient.from_env", side_effect=ValueError("unset")):
        assert sync_tripit.main() == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_unexpected_health_defect_still_emits_safe_shape(state_root: Path, capsys):
    """#327 review: a defect in the health bookkeeping inside the outer
    handler must not escape it — the scheduler reads a non-zero exit or
    invalid stdout as a silent skip."""
    with (
        patch("sync_tripit.ByAirClient.from_env", side_effect=ValueError("unset")),
        patch("sync_tripit._plan_sync_health", side_effect=KeyError("count")),
    ):
        assert sync_tripit.main() == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["wake_agent"] is False
    assert payload["data"]["error"] == "sync_exception"
    assert payload["data"]["health_error"] == "KeyError"


def test_local_fault_alerts_as_internal_not_byair(state_root: Path, capsys):
    """An exception escaping the sync (e.g. BYAIR_MCP_URL unset) is a local
    fault: it runs its own counter and never reads as a byAir outage."""
    payloads = [
        _internal_failure_payload(capsys) for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD)
    ]
    assert payloads[-1]["data"]["error"] == "sync_exception"
    assert _source_events(payloads[-1]) == [
        {
            "reason": "sync_internal_error",
            "consecutive_failures": sync_tripit._OUTAGE_ALERT_THRESHOLD,
            "error": "ValueError",
        }
    ]
    recovered = _main_payload(capsys, list_trips_return={"trips": []})
    assert _source_events(recovered) == [
        {
            "reason": "sync_internal_recovered",
            "failures_before_recovery": sync_tripit._OUTAGE_ALERT_THRESHOLD,
        }
    ]


def test_local_fault_leaves_the_byair_run_untouched(state_root: Path, capsys):
    """byAir was never asked on a local fault, so its run neither resets nor
    advances: two byAir failures, a local fault, then a third byAir failure
    still alerts."""
    failure = sync_tripit.ByAirError("session_missing", "no mcp-session-id")
    for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD - 1):
        _main_payload(capsys, list_trips_side_effect=failure)
    assert _source_events(_internal_failure_payload(capsys)) == []
    alert = _main_payload(capsys, list_trips_side_effect=failure)
    assert _source_events(alert) == [
        {
            "reason": "byair_unreachable",
            "consecutive_failures": sync_tripit._OUTAGE_ALERT_THRESHOLD,
            "error": "session_missing",
        }
    ]


@pytest.mark.parametrize(
    "trips_payload",
    [
        [],
        {},
        {"trips": None},
        {"trips": "nope"},
        {"trips": [["not", "a", "trip"]]},
        {"trips": [{"id": 1}]},
        {"trips": [{"flights": [7]}]},
        {"trips": [{"flights": [{"code": "XX1"}]}]},
    ],
    ids=[
        "list",
        "trips-missing",
        "trips-null",
        "trips-not-list",
        "trip-not-object",
        "flights-missing",
        "flight-not-object",
        "flight-id-missing",
    ],
)
def test_malformed_list_trips_counts_as_byair_failure(state_root: Path, capsys, trips_payload):
    """#327 review: byAir answering in the wrong shape is a byAir failure,
    so it advances byair_unreachable, not the internal run, and never reads
    as "no flights" (which would untrack every active flight)."""
    write_active_flights([100])
    payloads = [
        _main_payload(capsys, list_trips_return=trips_payload)
        for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD)
    ]
    assert payloads[-1]["data"]["error"] == "malformed_response"
    assert _source_events(payloads[-1]) == [
        {
            "reason": "byair_unreachable",
            "consecutive_failures": sync_tripit._OUTAGE_ALERT_THRESHOLD,
            "error": "malformed_response",
        }
    ]
    assert read_active_flights() == [100]


def test_internal_recovery_then_byair_failure_reports_both_runs(state_root: Path, capsys):
    """#327 review: clearing the local fault says nothing about byAir. A pass
    that completes locally but fails at byAir emits sync_internal_recovered
    and still advances the byAir run."""
    for _ in range(sync_tripit._OUTAGE_ALERT_THRESHOLD):
        _internal_failure_payload(capsys)
    failure = sync_tripit.ByAirError("session_missing", "no mcp-session-id")
    payload = _main_payload(capsys, list_trips_side_effect=failure)
    assert payload["data"]["error"] == "session_missing"
    assert _source_events(payload) == [
        {
            "reason": "sync_internal_recovered",
            "failures_before_recovery": sync_tripit._OUTAGE_ALERT_THRESHOLD,
        }
    ]
    record = json.loads((state_root / "sync-health.json").read_text(encoding="utf-8"))
    assert record["consecutive_failures"] == 1
    assert record["internal_failures"] == 0


def _health_record(**overrides) -> dict:
    record = {
        "schema_version": 1,
        "consecutive_failures": 1,
        "alerted": False,
        "last_error": "transport_error",
        "internal_failures": 0,
        "internal_alerted": False,
        "last_internal_error": None,
    }
    record.update(overrides)
    return record


_MISSING_LAST_ERROR = _health_record()
del _MISSING_LAST_ERROR["last_error"]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps(_health_record(schema_version=2)),
        json.dumps(_health_record(schema_version=True)),
        json.dumps(_health_record(schema_version=1.0)),
        # Valid types but no last_error key (Copilot on #327).
        json.dumps(_MISSING_LAST_ERROR),
    ],
    ids=["unparseable", "newer-schema", "bool-schema", "float-schema", "missing-last-error"],
)
def test_unusable_health_file_is_reported_not_reset(state_root: Path, capsys, content: str):
    """An unusable record is never silently zeroed: the pass reports
    `health_error`, leaves the file as found, and still emits its payload."""
    state_root.mkdir(parents=True)
    health = state_root / "sync-health.json"
    health.write_text(content, encoding="utf-8")
    failure = sync_tripit.ByAirError("session_missing", "no mcp-session-id")
    payload = _main_payload(capsys, list_trips_side_effect=failure)
    assert payload["data"]["error"] == "session_missing"
    assert payload["data"]["health_error"] == "StateError"
    assert _source_events(payload) == []
    assert health.read_text(encoding="utf-8") == content


def test_health_write_failure_keeps_the_sync_diff(state_root: Path, capsys):
    """The index already holds this pass's changes, so a failed health write
    must not cost the payload its tracked_flight_added event."""
    state_root.mkdir(parents=True)
    # A prior failure makes the clean pass below try to reset the counter.
    (state_root / "sync-health.json").write_text(json.dumps(_health_record()), encoding="utf-8")
    with (
        patch("sync_tripit.write_sync_health", side_effect=OSError("disk full")),
        patch("sync_tripit.ByAirClient.from_env") as mock_byair,
    ):
        mock_byair.return_value.list_trips.return_value = _trips_payload([_flight(100)])
        assert sync_tripit.main() == 0
    out, err = capsys.readouterr()
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["wake_agent"] is True
    assert [e["event"]["reason"] for e in payload["data"]["events"]] == ["tracked_flight_added"]
    # The write runs after emission, so its failure goes to stderr; the
    # unchanged record makes the next pass derive the same events.
    assert "not updated (disk full)" in err
    on_disk = json.loads((state_root / "sync-health.json").read_text(encoding="utf-8"))
    assert on_disk == _health_record()
    assert read_active_flights() == [100]


# ---------------------------------------------------------------------------
# Script-level subprocess test
# ---------------------------------------------------------------------------


def test_script_emits_safe_shape_when_byair_unset(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    env = {
        "FLIGHT_ASSIST_STATE_DIR": str(state),
        "PATH": "/usr/bin:/bin",
        # BYAIR_MCP_URL deliberately unset
    }
    script = REPO_ROOT / "skills" / "flight-assist" / "sync_tripit.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    last_line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(last_line)
    assert payload["wake_agent"] is False
    # Error context is in stderr; stdout stays a safe shape
    assert payload["data"].get("error") == "sync_exception"


@pytest.mark.parametrize(
    "override",
    [{"alerted": 1}, {"internal_alerted": "yes"}, {"last_error": 5}, {"last_internal_error": []}],
    ids=["alerted-int", "internal-alerted-str", "last-error-int", "last-internal-error-list"],
)
def test_writer_rejects_values_its_reader_would_refuse(state_root: Path, override: dict):
    """#327 review: the writer enforces the same field types the reader
    validates, so it can never persist a record the next run rejects."""
    from state import write_sync_health

    record = {k: v for k, v in _health_record(**override).items() if k != "schema_version"}
    with pytest.raises(ValueError):
        write_sync_health(record)
    assert not (state_root / "sync-health.json").exists()


# Completed removals are ordinary lifecycle, not owner notifications (#363).
_REMOVAL_NOW = datetime(2024, 1, 15, 13, tzinfo=UTC)


def _removed_payload(capsys, *, added=None):
    with patch("sync_tripit.datetime") as clock:
        clock.now.return_value = _REMOVAL_NOW
        return _main_payload(capsys, list_trips_return=_trips_payload(added or []))


def _seed_removal(*, scheduled="2024-01-15T12:00:00+00:00", snapshot=None, ledger=False):
    flight = _flight(100)
    flight.update(scheduledDepTime="2024-01-15T08:00:00+00:00", scheduledArrTime=scheduled)
    sync_tripit.initialize_flight_from_byair(flight=flight, now_utc=_REMOVAL_NOW)
    write_active_flights([100])
    if snapshot is not None or ledger:
        row = must(read_flight_state(100))
        if snapshot is not None:
            row["last_snapshot"] = snapshot
        if ledger:
            row["calendar_events"] = _ledger_entry()
        write_flight_state(row)


@pytest.mark.parametrize(
    ("scheduled", "snapshot", "wake"),
    [
        ("2024-01-15T06:00:00-06:00", None, False),
        ("2024-01-16T02:00:00+14:00", None, False),
        ("2024-01-16T04:00:00+14:00", None, True),
        ("2024-01-15T13:00:00+00:00", None, True),
        ("2024-01-15T12:00:00+00:00", {"arr_time": "2024-01-15T14:00:00Z"}, True),
        ("2024-01-15T14:00:00+00:00", {"arr_time": "2024-01-15T12:00:00Z"}, False),
        ("2024-01-15T12:00:00+00:00", {"arr_time": "2024-01-15T12:00:00"}, True),
        ("2024-01-15T12:00:00+00:00", {"arr_time": "invalid"}, True),
        ("2024-01-15T12:00:00", None, True),
        ("invalid", None, True),
        ("", None, True),
    ],
    ids=[
        "past-offset",
        "past-across-local-date",
        "future-across-local-date",
        "arrival-now",
        "delayed-in-air",
        "actual-arrival-wins",
        "unknown-actual-offset",
        "bad-actual",
        "unknown-scheduled-offset",
        "bad-scheduled",
        "missing-scheduled",
    ],
)
def test_removed_flight_wakes_only_when_completion_is_not_proven(
    state_root, capsys, scheduled, snapshot, wake
):
    _seed_removal(scheduled=scheduled, snapshot=snapshot)
    payload = _removed_payload(capsys)
    assert payload["wake_agent"] is wake
    events = payload["data"]["events"]
    assert [row["event"]["reason"] for row in events] == (
        ["tracked_flight_removed"] if wake else []
    )
    assert read_active_flights() == []
    assert read_flight_state(100) is None  # Lifecycle cleanup is independent of notification.


def test_quiet_completed_removal_preserves_exact_calendar_tombstone(state_root, capsys):
    _seed_removal(ledger=True)
    file = state_root / "flight-100.json"
    before, meta = file.read_bytes(), file.stat()
    payload = _removed_payload(capsys)
    assert payload["wake_agent"] is False and payload["data"]["events"] == []
    assert read_active_flights() == []
    assert file.read_bytes() == before and file.stat().st_ino == meta.st_ino
    assert file.stat().st_mtime_ns == meta.st_mtime_ns
    assert must(read_flight_state(100))["calendar_events"] == _ledger_entry()


def test_unknown_missing_removed_state_stays_actionable(state_root, capsys):
    write_active_flights([100])
    payload = _removed_payload(capsys)
    assert payload["wake_agent"] is True
    assert payload["data"]["events"][0]["event"]["reason"] == "tracked_flight_removed"
    assert read_active_flights() == []


def test_quiet_completed_removal_does_not_mask_an_added_replacement(state_root, capsys):
    _seed_removal()
    replacement = _flight(200)
    replacement.update(
        scheduledDepTime="2024-01-16T08:00:00Z", scheduledArrTime="2024-01-16T12:00:00Z"
    )
    payload = _removed_payload(capsys, added=[replacement])
    assert payload["wake_agent"] is True
    assert [row["event"]["reason"] for row in payload["data"]["events"]] == ["tracked_flight_added"]
    assert read_active_flights() == [200] and read_flight_state(100) is None
