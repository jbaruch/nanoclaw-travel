"""precheck._deliver: notices go out through the host and are confirmed only
on its success (#370). The journal is the real owner library."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[1] / "skills/flight-assist"
sys.path.insert(0, str(BUNDLE))

# Insert the shipped bundle path before importing its modules.
import precheck  # noqa: E402
from host_send import SendError  # noqa: E402
from notification_delivery import notification_store  # noqa: E402

NOW = datetime(2026, 11, 2, 16, 0, tzinfo=UTC)
STATE = {"code": "DL1234", "last_snapshot": {"dep_gate": "B12"}}


@pytest.fixture
def due(tmp_path, monkeypatch):
    """One offered boarding notice, already due, for flight 42."""
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(precheck, "read_flight_state", lambda fid: STATE)
    with notification_store(now_utc=NOW) as store:
        store.offer(
            42, {"reason": "boarding_started"}, prior_poll=None, expires_at=NOW + timedelta(hours=2)
        )
        return store.due(active_ids={42}, excluded_ids=set())


def confirmed(tmp_path) -> list:
    data = json.loads((tmp_path / "notifications.json").read_text())
    return [row["confirmed_at"] for row in data["notices"].values()]


def deliver(events, send, time_left=lambda: 60.0):
    return precheck._deliver(
        events, now_utc=NOW, time_left=time_left, send=send, read_calendar=lambda fid: None
    )


def test_a_sent_notice_is_confirmed(due, tmp_path):
    sent = []
    assert deliver(due, sent.append) == {"sent": 1, "send_failed": []}
    assert sent == ["Boarding now: DL1234. Gate B12."]
    assert confirmed(tmp_path) != [None]


def test_a_refused_send_stays_pending(due, tmp_path, capsys):
    def refuse(text):
        raise SendError("the host refused the send")

    result = deliver(due, refuse)
    assert result == {"sent": 0, "send_failed": [due[0]["notification_id"]]}
    assert confirmed(tmp_path) == [None]
    assert "notice not sent" in capsys.readouterr().err


def test_no_time_left_leaves_the_notice_for_the_next_cycle(due, tmp_path):
    sent = []
    result = deliver(due, sent.append, time_left=lambda: 1.0)
    assert sent == [] and result["send_failed"] == [due[0]["notification_id"]]
    assert confirmed(tmp_path) == [None]


def test_the_precheck_wakes_nobody_and_reports_delivery(monkeypatch, capsys):
    events = [{"flight_id": None, "event": {"reason": "byair_recovered"}}]
    monkeypatch.setattr(
        precheck, "evaluate_trip_window", lambda now_utc: type("W", (), {"in_window": True})()
    )
    monkeypatch.setattr(precheck, "_run_cycle", lambda **kwargs: events)
    monkeypatch.setattr(
        precheck, "_deliver", lambda events, **kwargs: {"sent": 1, "send_failed": []}
    )
    monkeypatch.setattr(precheck, "_reconcile", lambda: "ok")
    assert precheck.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "wake_agent": False,
        "data": {"events": events, "sent": 1, "send_failed": [], "reconcile": "ok"},
    }


def test_no_calendar_read_without_time_for_it_and_a_send(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(precheck, "read_flight_state", lambda fid: STATE)
    with notification_store(now_utc=NOW) as store:
        store.offer(
            42,
            {"reason": "day_before", "day_label": "tomorrow"},
            prior_poll=None,
            expires_at=NOW + timedelta(hours=2),
        )
        events = store.due(active_ids={42}, excluded_ids=set())
    reads = []

    def calendar(fid):
        reads.append(fid)
        return None

    tight = precheck._DAY_BEFORE_TIMEOUT_SECONDS + precheck.REPLY_TIMEOUT_SECONDS - 1
    sent = []
    precheck._deliver(
        events, now_utc=NOW, time_left=lambda: tight, send=sent.append, read_calendar=calendar
    )
    assert reads == [] and len(sent) == 1
    precheck._deliver(
        events, now_utc=NOW, time_left=lambda: 60.0, send=sent.append, read_calendar=calendar
    )
    assert reads == [42]
