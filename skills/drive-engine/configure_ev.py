#!/usr/bin/env python3
"""Record complete, owner-grounded EV preferences through the config owner.

Usage: configure_ev.py JSON_PREFERENCES
Stdout: {schema_version:1, recorded:bool}; stderr: fixed diagnostic on failure.
No provider request, vehicle command, calendar write or grant occurs here.
Preserves all other existing configuration fields. See ev-planning-contract.md.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_FA = Path("/home/node/.claude/skills/tessl__flight-assist")
if not _FA.is_dir():
    _FA = Path(__file__).resolve().parent.parent / "flight-assist"
if str(_FA) not in sys.path:
    sys.path.insert(0, str(_FA))

from ev_preferences import validate_ev_preferences  # noqa: E402
from state import StateError, read_config, write_config  # noqa: E402


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate preference key.")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Supply one bounded complete EV preference object.", file=sys.stderr)
        return 2
    try:
        if len(arguments[0].encode("utf-8")) > 256 * 1024:
            raise ValueError("Preference input exceeds its bound.")
        preferences = validate_ev_preferences(json.loads(arguments[0], object_pairs_hook=_unique))
        current = read_config() or {}
        old_mask = os.umask(0o077)
        try:
            write_config({**current, "ev_planning": preferences})
        finally:
            os.umask(old_mask)
    except (OSError, ValueError, StateError):
        print(
            "EV preferences not recorded. Inspect the owner config/schema and grounded "
            "vehicle/charging inputs; preserve the existing file. No provider action occurred.",
            file=sys.stderr,
        )
        print(json.dumps({"schema_version": 1, "recorded": False}))
        return 1
    print(json.dumps({"schema_version": 1, "recorded": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
