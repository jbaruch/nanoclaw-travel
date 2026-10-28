"""JSON-stdin owner intake for conference attendance; see attendance-contract.md."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from conference_attendance import MAX_BYTES, decode_store, record_attendance  # noqa: E402


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise ValueError("no arguments")
        # Reuse the exact store decoder, including duplicate/version/bounds checks.
        rows = decode_store(sys.stdin.buffer.read(MAX_BYTES + 1))
        if len(rows) != 1:
            raise ValueError("one row required")
        row = rows[0]
        record_attendance(
            {
                "id": row.id,
                "starts_on": row.starts_on,
                "ends_on": row.ends_on,
                "time_zone": row.time_zone,
                "venue_locations": list(row.venue_locations),
            }
        )
    # outer-boundary-process-contract: the skill consumes structured stdout;
    # emit a fixed error plus stderr, as a traceback would break its JSON contract.
    except Exception:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "recorded": False,
                    "error": "Attendance not recorded; follow attendance-contract.md",
                }
            )
        )
        print(
            "Attendance intake failed; verify the documented input and owner state", file=sys.stderr
        )
        return 1
    print(json.dumps({"schema_version": 1, "recorded": True, "id": row.id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
