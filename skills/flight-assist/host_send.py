"""Send one chat message through the NanoClaw host and return its own result (#370).

The script hands the text to the host exactly as the `send_message` tool
does: a `message` request in `/workspace/ipc/messages/`. It then waits for
the host's reply in this session's input directory. `{"stdout": ...}` is a
send the host confirmed; anything else, or no reply in time, raises
SendError. The notice then stays pending and its journal re-offers it later.
The model takes no part in delivery.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

IPC_ROOT = Path("/workspace/ipc")
REPLY_TIMEOUT_SECONDS = 15.0
MAX_TEXT_CHARS = 4000


class SendError(Exception):
    """The host did not confirm the send."""


def send_message(
    text: str,
    *,
    root: Path = IPC_ROOT,
    timeout: float = REPLY_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    chat, folder = os.environ.get("NANOCLAW_CHAT_JID"), os.environ.get("NANOCLAW_GROUP_FOLDER")
    if not chat or not folder:
        raise SendError("NANOCLAW_CHAT_JID/NANOCLAW_GROUP_FOLDER are unset; run inside a chat")
    if not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise SendError(f"a notice must be 1..{MAX_TEXT_CHARS} characters")
    request_id = uuid.uuid4().hex
    request = {
        "type": "message",
        "chatJid": chat,
        "groupFolder": folder,
        "text": text,
        "requestId": request_id,
        "sessionName": os.environ.get("NANOCLAW_SESSION_NAME", "default"),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    outbox = root / "messages"
    temporary = outbox / f".flight-{request_id}.tmp"
    # The message holds private chat text: create it exclusively at 0600 and
    # never leave a temporary copy behind.
    try:
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(request, ensure_ascii=False))
        temporary.rename(outbox / f"flight-{request_id}.json")
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise SendError(f"cannot queue the message for the host: {error}") from None
    reply = root / "input" / f"_script_result_{request_id}.json"
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            result = json.loads(reply.read_text(encoding="utf-8"))
        except FileNotFoundError:
            sleep(0.25)
            continue
        except (OSError, ValueError) as error:
            raise SendError(f"unreadable host reply: {error}") from None
        try:
            reply.unlink()
        except OSError as error:
            # The input mount is read-only for some sessions; the host sweeps
            # those replies. Any other unlink failure is a real fault.
            if error.errno not in (errno.EROFS, errno.EACCES):
                raise SendError(f"cannot remove the host reply: {error}") from None
            print(f"host_send: reply kept for the host sweep ({error.strerror})", file=sys.stderr)
        if isinstance(result, dict) and "stdout" in result:
            return
        raise SendError(f"the host refused the send: {str(result)[:300]}")
    raise SendError(f"no host reply within {timeout:.0f}s; the send is unconfirmed")
