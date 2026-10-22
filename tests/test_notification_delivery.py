"""Notice attempts persist independently of actual confirmed delivery."""

from __future__ import annotations

import io
import json
import re
import runpy
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from helpers import must

BUNDLE = Path(__file__).resolve().parents[1] / "skills/flight-assist"
sys.path.insert(0, str(BUNDLE))

# Insert the shipped bundle path before importing its module.
from notification_delivery import DeliveryError, notification_store  # noqa: E402

NOW = datetime(2025, 5, 18, 10, 0, tzinfo=UTC)
EVENT = {"reason": "gate_assignment", "dep_gate": "A1"}


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIGHT_ASSIST_STATE_DIR", str(tmp_path))
    return tmp_path


def offer(store, event=None, prior_poll="2025-05-18T09:00:00+00:00"):
    return store.offer(
        42,
        EVENT if event is None else event,
        prior_poll=prior_poll,
        expires_at=NOW + timedelta(hours=2),
    )


def due(store):
    return store.due(active_ids={42}, excluded_ids=set())


def test_offering_is_durable_but_never_claims_delivery(store_root):
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
        persisted = json.loads((store_root / "notifications.json").read_text())
        assert next(iter(persisted["notices"].values()))["id"] == identity
        assert store.confirmed_markers(42) == set()
        assert due(store) == [{"flight_id": 42, "event": EVENT, "notification_id": identity}]
        assert store.confirmed_markers(42) == set()
    assert (store_root / "notifications.json").stat().st_mode & 0o777 == 0o600


def test_an_unconfirmed_attempt_retries_with_the_same_id_after_backoff(store_root):
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
        assert due(store)[0]["notification_id"] == identity
    with notification_store(now_utc=NOW + timedelta(seconds=299)) as store:
        assert due(store) == []
    with notification_store(now_utc=NOW + timedelta(seconds=300)) as store:
        assert due(store)[0]["notification_id"] == identity
        assert store.confirmed_markers(42) == set()


def test_confirmed_delivery_suppresses_retries_and_sets_only_its_phase(store_root):
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
        assert store.confirm([identity, identity]) == {"confirmed": [identity], "stale": []}
        assert store.confirm([identity]) == {"confirmed": [identity], "stale": []}
    with notification_store(now_utc=NOW + timedelta(minutes=10)) as store:
        assert due(store) == []
        assert store.confirmed_markers(42) == {"gate_assignment_fired"}
        assert offer(store, {"reason": "gate_assignment", "dep_gate": "B2"}) is None


def test_changed_pending_notice_cannot_be_consumed_by_its_older_ack(store_root):
    with notification_store(now_utc=NOW) as store:
        old = must(offer(store))
        current = must(offer(store, {"reason": "gate_assignment", "dep_gate": "B2"}))
        assert old != current
        assert store.confirm([old]) == {"confirmed": [], "stale": [old]}
        assert store.confirmed_markers(42) == set()
        assert due(store)[0]["notification_id"] == current
        assert store.confirm([current])["confirmed"] == [current]


def test_repeated_delta_after_a_later_poll_is_a_new_observation(store_root):
    event = {"reason": "gate_change", "from": "A1", "to": "B2"}
    with notification_store(now_utc=NOW) as store:
        first = must(offer(store, event))
        assert offer(store, event) == first
        store.confirm([first])
        assert offer(store, event) is None
        later = must(offer(store, event, prior_poll="2025-05-18T09:30:00+00:00"))
        assert later != first
        assert due(store)[0]["notification_id"] == later
        assert store.confirmed_markers(42) == set()


def test_unverified_flight_is_deferred_and_inactive_or_expired_notices_are_dropped(store_root):
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
        assert store.due(active_ids={42}, excluded_ids={42}) == []
        assert due(store)[0]["notification_id"] == identity
    with notification_store(now_utc=NOW + timedelta(hours=2)) as store:
        assert due(store) == []
        assert store.confirm([identity])["stale"] == [identity]
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
        assert store.due(active_ids=set(), excluded_ids=set()) == []
        assert store.confirm([identity])["stale"] == [identity]


@pytest.mark.parametrize("version", [0, 2, True])
def test_unsupported_store_is_not_replaced_by_an_empty_record(store_root, version):
    target = store_root / "notifications.json"
    original = json.dumps({"schema_version": version, "sequence": 0, "notices": {}})
    target.write_text(original)
    with pytest.raises(DeliveryError, match="do not delete its delivery receipts"):
        with notification_store(now_utc=NOW):
            pytest.fail("An unsupported store must not be opened.")
    assert target.read_text() == original


def test_input_and_returned_payload_mutations_do_not_rewrite_the_private_notice(store_root):
    event = dict(EVENT)
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store, event))
        event["dep_gate"] = "uncommitted"
        returned = due(store)
        returned[0]["event"]["dep_gate"] = "uncommitted"
        store.confirm([identity])
    data = json.loads((store_root / "notifications.json").read_text())
    assert next(iter(data["notices"].values()))["event"]["dep_gate"] == "A1"


def test_a_second_process_cannot_modify_the_same_store_while_locked(store_root):
    code = (
        "import sys; from datetime import UTC, datetime; "
        "sys.path.insert(0, sys.argv[1]); "
        "from notification_delivery import notification_store, DeliveryBusy\n"
        "try:\n"
        " with notification_store(now_utc=datetime(2025,5,18,10,tzinfo=UTC)): print('opened')\n"
        "except DeliveryBusy:\n"
        " print('busy'); sys.exit(3)\n"
    )
    with notification_store(now_utc=NOW):
        blocked = subprocess.run(
            [sys.executable, "-c", code, str(BUNDLE)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert blocked.returncode == 3, blocked.stderr
        assert blocked.stdout.strip() == "busy"
    opened = subprocess.run(
        [sys.executable, "-c", code, str(BUNDLE)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert opened.returncode == 0, opened.stderr
    assert opened.stdout.strip() == "opened"


def test_connection_retry_refreshes_both_legs_and_defers_on_either_failed_poll(store_root):
    event = {
        "reason": "connection_at_risk",
        "leg1_flight_id": 7,
        "leg1_code": "AA1",
        "leg2_code": "AA2",
    }
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store, event))
        assert store.retry_due_ids() == {7, 42}
        assert store.due(active_ids={7, 42}, excluded_ids={7}) == []
        assert store.confirmed_markers(42) == set()
        assert store.due(active_ids={7, 42}, excluded_ids=set())[0]["notification_id"] == identity
    with notification_store(now_utc=NOW + timedelta(minutes=5)) as store:
        assert store.retry_due_ids() == {7, 42}
        assert store.due(active_ids={7, 42}, excluded_ids=set(), removed_ids={7}) == []
        assert store.confirm([identity])["stale"] == [identity]


def test_obsolete_phase_is_removed_only_when_every_dependency_was_refreshed(store_root):
    event = {"reason": "connection_at_risk", "leg1_flight_id": 7}
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store, event))
        store.reconcile(
            verified_ids={42}, current_phases=set(), is_current=lambda _id, _event: True
        )
        assert len(store.data["notices"]) == 1
        store.reconcile(
            verified_ids={7, 42}, current_phases=set(), is_current=lambda _id, _event: True
        )
        assert store.confirm([identity])["stale"] == [identity]
        assert store.confirmed_markers(42) == set()


def test_changed_live_state_can_supersede_a_pending_delta_without_a_false_receipt(store_root):
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store, {"reason": "gate_change", "side": "dep", "to": "B2"}))
        store.reconcile(
            verified_ids={42}, current_phases=set(), is_current=lambda _id, _event: False
        )
        assert store.confirm([identity])["stale"] == [identity]
        assert store.confirmed_markers(42) == set()


def test_confirmation_cli_consumes_only_the_exact_confirmed_send_ids(
    store_root, monkeypatch, capsys
):
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
    module = runpy.run_path(str(BUNDLE / "scripts/confirm-notifications.py"))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(json.dumps({"notification_ids": [identity]}).encode())),
    )
    assert module["main"](now_utc=NOW) == 0
    assert json.loads(capsys.readouterr().out) == {"confirmed": [identity], "stale": []}
    with notification_store(now_utc=NOW) as store:
        assert store.confirmed_markers(42) == {"gate_assignment_fired"}


def test_pruning_reclaims_inactive_and_expired_attempts_but_keeps_active_receipts(store_root):
    with notification_store(now_utc=NOW) as store:
        confirmed = must(offer(store))
        store.confirm([confirmed])
        pending = must(offer(store, {"reason": "delay", "new_dep_time": "2025-05-18T12:00Z"}))
    with notification_store(now_utc=NOW + timedelta(hours=3)) as store:
        store.prune(active_ids={42})
        assert store.confirm([confirmed, pending]) == {"confirmed": [confirmed], "stale": [pending]}
        assert store.confirmed_markers(42) == {"gate_assignment_fired"}
        store.prune(active_ids=set())
        assert store.confirm([confirmed])["stale"] == [confirmed]


def test_full_store_refuses_new_notices_without_erasing_pending_work(store_root, monkeypatch):
    monkeypatch.setattr("notification_delivery.MAX_NOTICES", 1)
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
        with pytest.raises(DeliveryError, match="do not erase receipts"):
            offer(store, {"reason": "cancelled"})
        assert due(store)[0]["notification_id"] == identity
        assert store.confirm([identity])["confirmed"] == [identity]


@pytest.mark.parametrize(
    "lookup",
    [
        "/home/node/.claude/skills/tessl__flight-assist/scripts/confirm-notifications.py",
        "skills/flight-assist/scripts/confirm-notifications.py",
        ".tessl/plugins/jbaruch/nanoclaw-travel/skills/flight-assist/scripts/confirm-notifications.py",
    ],
)
def test_declared_lookup_runs_the_co_shipped_helper_from_an_unrelated_directory(
    store_root, tmp_path, lookup
):
    contract = (BUNDLE.parents[1] / "rules/flight-notification-delivery.md").read_text()
    declared = set(re.findall(r"`([^`]*confirm-notifications\.py)`", contract))
    assert lookup in declared
    reference = (BUNDLE / "references/notification-delivery.md").read_text()
    match = re.search(
        r"python3 (/home/node/\.claude/skills/"
        r"tessl__flight-assist/scripts/confirm-notifications\.py)",
        reference,
    )
    assert match is not None
    if lookup.startswith("/"):
        assert lookup == match[1]
    native = tmp_path / "installation" / lookup.lstrip("/")
    shutil.copytree(BUNDLE, native.parents[1], ignore=shutil.ignore_patterns("__pycache__"))
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
    reply = subprocess.run(
        [sys.executable, str(native)],
        cwd=tmp_path,
        input=json.dumps({"notification_ids": [identity]}),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert reply.returncode == 0, reply.stderr
    assert json.loads(reply.stdout) == {"confirmed": [identity], "stale": []}
    bad = subprocess.run(
        [sys.executable, str(native)],
        cwd=tmp_path,
        input="{}",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert bad.returncode == 2 and bad.stdout == ""
    assert "without resending" in bad.stderr
    (native.parents[1] / "notification_delivery.py").unlink()
    missing = subprocess.run(
        [sys.executable, str(native)],
        cwd=tmp_path,
        input="{}",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert missing.returncode == 2 and missing.stdout == ""
    assert "reinstall the complete" in missing.stderr


def test_replacing_pending_payload_keeps_the_existing_retry_deadline(store_root):
    with notification_store(now_utc=NOW) as store:
        old = must(offer(store))
        assert due(store)[0]["notification_id"] == old
    with notification_store(now_utc=NOW + timedelta(minutes=2)) as store:
        current = must(offer(store, {"reason": "gate_assignment", "dep_gate": "B2"}))
        assert current != old
        assert due(store) == []
        assert store.confirm([old])["stale"] == [old]
    with notification_store(now_utc=NOW + timedelta(minutes=5)) as store:
        assert due(store)[0]["notification_id"] == current


@pytest.mark.parametrize(
    "payload", [{}, {"notification_ids": "not a list"}, {"notification_ids": [True]}]
)
def test_confirmation_cli_refuses_invalid_input_without_consuming_pending_notice(
    store_root, monkeypatch, capsys, payload
):
    with notification_store(now_utc=NOW) as store:
        identity = must(offer(store))
    module = runpy.run_path(str(BUNDLE / "scripts/confirm-notifications.py"))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    assert module["main"](now_utc=NOW) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "without resending" in output.err
    with notification_store(now_utc=NOW) as store:
        assert due(store)[0]["notification_id"] == identity
