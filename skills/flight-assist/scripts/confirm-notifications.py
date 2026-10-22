#!/usr/bin/env python3
"""Confirm flight notifications only after send_message returned success.

stdin: {notification_ids: [exact ids carried by the wake events]}.
stdout: {confirmed: [...], stale: [...]}; stale ids never consume newer notices.
Exit 0 for a valid acknowledgement, 2 for malformed input or a store failure.
No message is sent and no historical phase marker becomes a receipt.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from notification_delivery import DeliveryError, notification_store
except ModuleNotFoundError as error:
    print(
        f"Flight notice dependency {error.name} is missing; reinstall the complete "
        "nanoclaw-travel flight-assist bundle and retry the acknowledgement without resending.",
        file=sys.stderr,
    )
    sys.exit(2)


def main(*, now_utc: datetime | None = None) -> int:
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("Reduce the acknowledgement to the ids from this confirmed send.")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"notification_ids"}:
            raise ValueError("Provide only notification_ids from confirmed sends.")
        if not isinstance(value["notification_ids"], list):
            raise ValueError("Provide notification_ids as a JSON array.")
        with notification_store(now_utc=now_utc or datetime.now(UTC)) as store:
            result = store.confirm(value["notification_ids"])
    except (OSError, DeliveryError, ValueError, TypeError, KeyError, RecursionError) as error:
        print(
            f"Cannot confirm flight delivery: {error}. "
            "Repair the store or read permissions and retry the acknowledgement without resending.",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
