"""JSON-stdin owner intake for conference attendance; see attendance-contract.md."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from conference_attendance import MAX_BYTES, AttendanceError, decode_store, record_attendance  # noqa: E402


def main() -> int:
    phase = "request_decode"
    try:
        if len(sys.argv) != 1:
            raise ValueError("no arguments")
        # Reuse the exact store decoder, including duplicate/version/bounds checks.
        rows = decode_store(sys.stdin.buffer.read(MAX_BYTES + 1))
        if len(rows) != 1:
            raise ValueError("one row required")
        row = rows[0]
        phase = "state_upsert"
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
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "recorded": False,
                    "error": "Attendance not recorded; follow attendance-contract.md",
                }
            )
        )
        if isinstance(exc, AttendanceError):
            cause = str(exc)  # Fixed owner messages contain no input values or paths.
        elif isinstance(exc, OSError):
            cause = f"filesystem errno={exc.errno}; verify agent access and storage health"
        elif isinstance(exc, ValueError):
            cause = "invalid intake envelope; provide exactly one row and no arguments"
        else:
            cause = "unexpected owner failure; inspect the installed utility before retrying"
        print(
            f"Attendance intake failed phase={phase} cause={type(exc).__name__}: {cause}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"schema_version": 1, "recorded": True, "id": row.id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
