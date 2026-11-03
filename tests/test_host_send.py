"""host_send.py: queue a message for the host and wait for its own result (#370).

The request copies the agent runner's `send_message` IPC shape; the reply
shape is the host's `{"stdout": ...}` / `{"error": ...}` script result.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[1] / "skills/flight-assist"
sys.path.insert(0, str(BUNDLE))

# Insert the shipped bundle path before importing its module.
from host_send import SendError, send_message  # noqa: E402


@pytest.fixture
def ipc(tmp_path, monkeypatch):
    (tmp_path / "messages").mkdir()
    (tmp_path / "input").mkdir()
    monkeypatch.setenv("NANOCLAW_CHAT_JID", "tg:-1003859124897")
    monkeypatch.setenv("NANOCLAW_GROUP_FOLDER", "telegram_swarm")
    monkeypatch.setenv("NANOCLAW_SESSION_NAME", "maintenance")
    return tmp_path


def host(ipc, reply):
    """A fake host: answer the first queued request with `reply`. Returns the
    thread and a dict that receives the request it saw."""
    seen: dict = {}

    def run():
        for _ in range(200):
            queued = list((ipc / "messages").glob("flight-*.json"))
            if queued:
                request = json.loads(queued[0].read_text())
                seen.update(request)
                (ipc / "input" / f"_script_result_{request['requestId']}.json").write_text(
                    json.dumps(reply)
                )
                return
            threading.Event().wait(0.01)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, seen


def test_a_confirmed_send_returns_and_removes_the_reply(ipc):
    thread, request = host(ipc, {"stdout": "sent"})
    send_message("Boarding now: DL1234. Gate B12.", root=ipc, timeout=5)
    thread.join()
    assert request["type"] == "message"
    assert request["text"] == "Boarding now: DL1234. Gate B12."
    assert (request["chatJid"], request["groupFolder"], request["sessionName"]) == (
        "tg:-1003859124897",
        "telegram_swarm",
        "maintenance",
    )
    assert list((ipc / "input").iterdir()) == []


def test_a_host_refusal_raises(ipc):
    thread, _request = host(ipc, {"error": "Message delivery could not be confirmed."})
    with pytest.raises(SendError, match="refused"):
        send_message("x", root=ipc, timeout=5)
    thread.join()


def test_no_reply_is_unconfirmed(ipc):
    clock = iter(range(100))
    with pytest.raises(SendError, match="unconfirmed"):
        send_message("x", root=ipc, timeout=3, monotonic=lambda: next(clock), sleep=lambda s: None)


def test_outside_a_chat_nothing_is_queued(ipc, monkeypatch):
    monkeypatch.delenv("NANOCLAW_CHAT_JID")
    with pytest.raises(SendError, match="NANOCLAW_CHAT_JID"):
        send_message("x", root=ipc)
    assert list((ipc / "messages").iterdir()) == []


def test_an_empty_notice_is_refused(ipc):
    with pytest.raises(SendError, match="characters"):
        send_message("  ", root=ipc)


def test_the_queued_message_is_private(ipc, monkeypatch):
    modes = []
    real_rename = Path.rename

    def watch(self, target):
        modes.append(self.stat().st_mode & 0o777)
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", watch)
    thread, _request = host(ipc, {"stdout": "sent"})
    send_message("Boarding now.", root=ipc, timeout=5)
    thread.join()
    assert modes == [0o600]


def test_a_failed_queue_leaves_no_temporary(ipc, monkeypatch):
    def refuse(self, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "rename", refuse)
    with pytest.raises(SendError, match="cannot queue"):
        send_message("Boarding now.", root=ipc, timeout=1)
    assert list((ipc / "messages").iterdir()) == []
