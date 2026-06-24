#!/usr/bin/env python3
"""Resolve the operator's current IANA timezone for surface phrasing.

flight-assist surfaces (day-before, arrival logistics, …) phrase
relative dates ("today" / "tomorrow") against the operator's local
date, not the container's UTC clock (`jbaruch/nanoclaw-admin#305`). This
helper reads the operator's `current_tz` so the surface composer can do
that, per `rules/operator-local-tz-phrasing.md`.

stdlib-only per `jbaruch/coding-policy: dependency-management`.

Source: the `tz_state` singleton at `/workspace/store/messages.db`
(host-owned, mounted RW in main/trusted containers). This overlay reads
that store directly — it does NOT depend on `nanoclaw-admin`'s
`heartbeat-precheck.json`, which need not be present.

Reader contract per `jbaruch/coding-policy: stateful-artifacts`: a
non-owner reader never migrates. A `tz_state.schema_version` other than
the supported value is treated as "no usable timezone" (fall through to
unavailable). Bump `SUPPORTED_TZ_STATE_SCHEMA_VERSION` in lock-step with
the host-side state-NNN migration that changes the `current_tz` shape.

Usage:
    read-current-tz.py        (no arguments)

Stdout (single-line JSON):
    {"available": true,  "tz": "<iana>"}   operator current_tz resolved
    {"available": false, "tz": null}       no usable current_tz

Exit code:
    0 always. This is a surface helper — a notification must still fire
    with explicit-date phrasing when the timezone can't be resolved, so
    every failure mode (missing DB, missing row, empty column, schema
    mismatch, unparseable zone) emits the safe `available: false` shape
    and a stderr diagnostic rather than aborting the surface. CLI misuse
    (extra args) is the one non-zero exit.
"""

import json
import sqlite3
import sys
import zoneinfo

DB_PATH = "/workspace/store/messages.db"

# Highest `tz_state.schema_version` this reader interprets. The host
# orchestrator owns `tz_state` writes; a higher version means a shape
# this reader doesn't understand, so it degrades to unavailable.
SUPPORTED_TZ_STATE_SCHEMA_VERSION = 4


def _emit(available: bool, tz: str | None) -> None:
    print(json.dumps({"available": available, "tz": tz}, separators=(",", ":")))


def resolve_current_tz() -> str | None:
    """Return the operator's `current_tz` IANA name, or None.

    None on every soft/hard miss: DB unreachable or corrupt, `tz_state`
    table or singleton row absent, `current_tz` empty, `schema_version`
    unsupported, or the stored name not a valid zoneinfo zone. Each miss
    writes a stderr diagnostic. `home_tz` is deliberately NOT a fallback
    — relative-date phrasing needs where the operator is *now*, so a
    missing current_tz degrades to explicit-date phrasing, never to a
    guessed home zone.
    """
    conn = None
    try:
        # Read-only URI open: a non-owner reader must never create the
        # host-owned messages.db (a plain connect() would materialise an
        # empty file when the mount is missing) nor mutate it. The store
        # dir is RW-mounted in main/trusted containers, so read-only
        # opens of the WAL database succeed; a missing file raises
        # OperationalError, caught below as a degrade-to-unavailable.
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT current_tz, schema_version FROM tz_state WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        print(f"read-current-tz: cannot read tz_state from {DB_PATH}: {exc}", file=sys.stderr)
        return None
    finally:
        if conn is not None:
            conn.close()

    if row is None:
        print(f"read-current-tz: tz_state has no singleton row at {DB_PATH}", file=sys.stderr)
        return None

    current_tz, schema_version = row
    if not isinstance(schema_version, int) or schema_version != SUPPORTED_TZ_STATE_SCHEMA_VERSION:
        print(
            f"read-current-tz: tz_state.schema_version={schema_version!r} unsupported "
            f"(reader supports {SUPPORTED_TZ_STATE_SCHEMA_VERSION})",
            file=sys.stderr,
        )
        return None

    if not isinstance(current_tz, str) or not current_tz.strip():
        print(f"read-current-tz: tz_state.current_tz empty at {DB_PATH}", file=sys.stderr)
        return None

    name = current_tz.strip()
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        print(f"read-current-tz: unrecognised timezone {name!r}: {exc}", file=sys.stderr)
        return None

    return name


def main() -> int:
    if len(sys.argv) != 1:
        print(f"Usage: {sys.argv[0]} (no arguments)", file=sys.stderr)
        return 2
    tz = resolve_current_tz()
    _emit(tz is not None, tz)
    return 0


if __name__ == "__main__":
    sys.exit(main())
