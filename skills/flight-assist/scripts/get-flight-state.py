#!/usr/bin/env python3
"""Read a per-flight state record and print it as JSON.

Used by SKILL.md Step 3 to enrich a wake-event notification with
the flight's last-known snapshot (gates, terminals, times, etc.)
without requiring the agent to embed a state-reader Python snippet
in its prompt.

Usage:
    get-flight-state.py <flight_id>

Output: single-line JSON on stdout with the full state record on
success, or `{"error": "..."}` on missing flight / invalid argument.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BUNDLE_DIR))

from state import read_flight_state  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            json.dumps({"error": "usage: get-flight-state.py <flight_id>"}),
            file=sys.stderr,
        )
        return 2
    try:
        flight_id = int(argv[1])
    except ValueError:
        print(
            json.dumps({"error": f"flight_id must be int, got {argv[1]!r}"}),
            file=sys.stderr,
        )
        return 2
    state = read_flight_state(flight_id)
    if state is None:
        print(json.dumps({"error": f"flight_id {flight_id} has no state on disk"}))
        return 0
    print(json.dumps(state, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
