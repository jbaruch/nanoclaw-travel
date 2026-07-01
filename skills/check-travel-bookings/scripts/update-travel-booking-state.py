#!/usr/bin/env python3
"""
Snooze / resolve a trip's booking-gap state under
`/workspace/group/travel-booking-state.json`.

Per `coding-policy: script-delegation`, deterministic JSON mutation
lives in scripts. The agent picks the slug + action (the fuzzy-match
from natural-language stays in the LLM's hands per the same rule);
this script handles read, mutate, and atomic write.

State file owner: `check-travel-bookings` (see sibling `state-schema.md`).
Every entry the script writes carries `schema_version: 1`.

Usage:
    update-travel-booking-state.py --slug <slug> --action snooze --until YYYY-MM-DD
    update-travel-booking-state.py --slug <slug> --action resolve

Output: single-line JSON to stdout `{"action": "...", "slug": "...",
"state": {<post-update state map>}}`. Errors go to stderr with non-
zero exit per `rules/file-hygiene.md` I/O conventions.
"""

import argparse
import json
import os
import sys
from datetime import date

STATE_PATH = "/workspace/group/travel-booking-state.json"
SCHEMA_VERSION = 1


def _read_state(path: str) -> dict:
    """Read the current state. Missing / unreadable / non-dict roots
    return an empty state — the file's contract is purely advisory
    snooze data, so absent/corrupt = no snoozes active.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _atomic_write(path: str, payload: dict) -> None:
    """Write payload to `path` atomically. Same-dir `.tmp` + `os.replace`
    matches the pattern in `skills/check-travel-bookings/scripts/build-
    travel-db.py` and `skills/flight-assist/state.py::_atomic_write_json`;
    file mode inherits process umask so cross-plugin readers (the same
    group-volume readers `build-travel-db.py` accommodates) keep their
    read access."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True, help="Trip slug, e.g. madrid-2026-06")
    parser.add_argument("--action", required=True, choices=("snooze", "resolve"))
    parser.add_argument("--until", help="ISO date for snooze (required for --action snooze)")
    parser.add_argument(
        "--state-path",
        default=STATE_PATH,
        help="Override state file path. Tests inject a tmp path; the "
        "production default is the module-level constant.",
    )
    args = parser.parse_args(argv)

    if args.action == "snooze":
        if not args.until:
            print(
                "update-travel-booking-state: --until is required for --action snooze",
                file=sys.stderr,
            )
            return 1
        try:
            date.fromisoformat(args.until)
        except ValueError:
            print(
                f"update-travel-booking-state: --until {args.until!r} is not a valid "
                "ISO date (YYYY-MM-DD)",
                file=sys.stderr,
            )
            return 1

    state = _read_state(args.state_path)

    if args.action == "snooze":
        state[args.slug] = {
            "schema_version": SCHEMA_VERSION,
            "snooze_until": args.until,
        }
    else:  # resolve
        state.pop(args.slug, None)

    try:
        _atomic_write(args.state_path, state)
    except OSError as exc:
        # PermissionError, ENOSPC, cross-device EXDEV, etc. surface
        # as a clean stderr diagnostic + non-zero exit instead of an
        # uncaught traceback. The mutation didn't land — `os.replace`
        # is atomic, so partial state is impossible.
        print(
            f"update-travel-booking-state: failed to write {args.state_path}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {"action": args.action, "slug": args.slug, "state": state},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
