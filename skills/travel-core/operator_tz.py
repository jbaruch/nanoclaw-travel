"""Read the operator's current IANA zone through the core `current-tz` reader.

`jbaruch/nanoclaw-core` hosts the one reader of the host's `tz_state` store,
`skills/current-tz/scripts/read-current-tz.py`, installed in every tier at the
runtime mount named below. This module is how a Python precheck in this plugin
asks it: spawn the script, parse its single-line JSON, hand back the zone. No
script here opens the store itself — the reader's no-guess contract (the zone
the host resolved from the operator's live location, or nothing; `home_tz`
never a fallback) is what keeps every surface agreeing on where the operator is.

Reader contract, restated only as far as this caller reads it (the docstring of
`read-current-tz.py` is authoritative): stdout is `{"available": true, "tz",
"local_now", "local_date"}` or the all-null `available: false` shape; exit 0
when the store was read, 1 when it could not be read (the unavailable shape is
still on stdout), 2 on CLI misuse.

Every unavailable outcome returns None with its own stderr line, so a caller
degrades to an explicit date or the event's own zone instead of the whole
cycle going dark: reader not installed, reader failed to run or timed out,
store unreadable, `available: false`, output this caller cannot parse. The
reader's own stderr is relayed verbatim so its diagnosis is not lost.

Consumers: flight-assist's `precheck` (the `day_before` day label, #300) and
the drive engine's `reconcile_sweep` (the meeting-drive display zone, #301).

stdlib-only per `coding-policy: dependency-management`.

Public API:
    from operator_tz import CORE_READER_PATH, OperatorTz, read_operator_tz

    zone = read_operator_tz()                      # OperatorTz | None
    zone = read_operator_tz(now=some_instant)      # local fields at that instant
    zone.tz, zone.local_now, zone.local_date
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The NanoClaw runtime mounts every `tessl__*` skill under this prefix; core's
# `current-tz` skill is installed in every tier (nanoclaw-travel 0.2.130).
CORE_READER_PATH = Path("/home/node/.claude/skills/tessl__current-tz/scripts/read-current-tz.py")

# The reader does one local sqlite read; anything slower is a hung process, and
# both consumers run under a host precheck kill budget.
READER_TIMEOUT_SECONDS = 5.0

RunFn = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class OperatorTz:
    """The reader's `available: true` payload.

    `tz` is the IANA name; `local_now` / `local_date` are the queried instant
    expressed in it (`YYYY-MM-DDTHH:MM:SS±HH:MM` / `YYYY-MM-DD`), so a caller
    that only needs the operator's date never converts by hand.
    """

    tz: str
    local_now: str
    local_date: str


def _unavailable(reason: str) -> None:
    print(f"operator_tz: {reason}; operator zone unavailable", file=sys.stderr)
    return None


def read_operator_tz(
    *,
    now: datetime | None = None,
    reader: Path = CORE_READER_PATH,
    run: RunFn = subprocess.run,
) -> OperatorTz | None:
    """Return the operator's current zone, or None when no usable zone came back.

    `now` pins the instant `local_now` / `local_date` describe (passed to the
    reader as `--now`); it must be timezone-aware, matching the reader's own
    rule. `reader` and `run` are injection points for tests; production callers
    take the defaults.
    """
    if now is not None and now.tzinfo is None:
        raise ValueError("read_operator_tz: `now` must be timezone-aware")
    if not reader.is_file():
        return _unavailable(
            f"core reader not installed at {reader} — install jbaruch/nanoclaw-core "
            "(skills/current-tz) in this tier"
        )
    argv = [sys.executable, str(reader)]
    if now is not None:
        argv += ["--now", now.isoformat()]
    try:
        proc = run(
            argv,
            capture_output=True,
            text=True,
            timeout=READER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _unavailable(f"core reader did not answer within {READER_TIMEOUT_SECONDS:.0f}s")
    except OSError as exc:
        return _unavailable(f"core reader could not be started ({exc})")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        if not proc.stderr.endswith("\n"):
            sys.stderr.write("\n")
    if proc.returncode not in (0, 1):
        # Exit 2 is CLI misuse — a contract break between two plugins we both
        # own, not an operational miss. Still a degrade, never a dark cycle.
        return _unavailable(f"core reader exited {proc.returncode} for {argv[2:]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _unavailable(f"core reader printed non-JSON stdout: {proc.stdout.strip()[:120]!r}")
    if not isinstance(payload, dict):
        return _unavailable("core reader stdout is not a JSON object")
    if payload.get("available") is not True:
        # The reader already said why on stderr (no row, empty zone, unsupported
        # schema, unreadable store); nothing to add.
        return None
    tz, local_now, local_date = (payload.get(k) for k in ("tz", "local_now", "local_date"))
    if not all(isinstance(v, str) and v for v in (tz, local_now, local_date)):
        return _unavailable(f"core reader payload is missing tz/local fields: {payload!r}")
    assert isinstance(tz, str) and isinstance(local_now, str) and isinstance(local_date, str)
    return OperatorTz(tz=tz, local_now=local_now, local_date=local_date)
