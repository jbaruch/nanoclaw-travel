#!/usr/bin/env python3
"""Persist the user's home address to the flight-assist plugin config.

Read by the precheck script for `time_to_leave` Distance Matrix
queries (the leave-by deadline is computed against current traffic
between this address and the departure airport).

Usage:
    set-home-base.py "1 Infinite Loop, Cupertino, CA 95014"

Idempotent — overwrites any prior `home_address` value. Preserves
other config keys.

Output: single-line JSON on stdout with `{"home_address": "..."}`
on success, or `{"error": "..."}` on validation failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BUNDLE_DIR))

from state import read_config, write_config  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            json.dumps({"error": "usage: set-home-base.py <home_address>"}),
            file=sys.stderr,
        )
        return 2
    home_address = argv[1].strip()
    if not home_address:
        print(json.dumps({"error": "home_address must not be empty"}), file=sys.stderr)
        return 2
    existing = read_config() or {}
    existing["home_address"] = home_address
    write_config(existing)
    print(json.dumps({"home_address": home_address}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
