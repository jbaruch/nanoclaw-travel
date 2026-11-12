"""Read-only adapter to talk-prep's Prep-window reader (#381) — the talk's arrival target.

A talk can carry a talk-prep `Prep` hold: a script-owned interval that ends at
the talk start, during which the owner wants to be at the venue already. The
drive to that talk should arrive at the hold's start, not the talk's. The hold
is owned by `jbaruch/nanoclaw-admin`'s talk-prep skill (main chat only), and its
contract forbids consumers from parsing the owner fragment or deriving the hold
ID. The one supported read is the provider's declared reader, so that is the
only thing this module executes.

Contract (the consumer side of talk-prep's `calendar-contract.md`):

- **Path.** Only the fixed native mount `READER` =
  `/home/node/.claude/skills/tessl__talk-prep/scripts/talk-prep.py`. Every path
  component from the skills mount down must be a real directory and the script a
  regular file; a symlink anywhere refuses the install. No environment variable
  or dev-clone fallback can redirect which executable runs (nanoclaw-admin is
  private and not a declared dev tile here).
- **Argv.** `[sys.executable, READER, "--prep-window", <talk event ID>]`, literal,
  no shell. An ID that does not fully match the provider's
  `[A-Za-z0-9_-]{1,1024}` is never sent: no call, no hold.
- **Bounds.** stdout and stderr are each capped at `MAX_OUTPUT` bytes and the
  call at `TIMEOUT` seconds. The sweep's plan phase has no
  deadline (`reconcile_sweep`), so this bound is what keeps a hung provider from
  stalling the sweep.
- **Output.** Exit 0 with exactly `{"hold": null}` (no hold, or a cancelled one)
  or `{"hold": {"id", "start", "end"}}` with timezone-aware RFC 3339 times and
  start < end. Duplicate keys, extra keys, a naive or unparseable time, a
  non-positive interval, a nonzero exit, a timeout or oversize output are all a
  read FAILURE (`PrepWindowUnavailable`), never "no hold".

Three install outcomes:

1. **Not installed** — no `tessl__talk-prep` entry in the mount at all. Talks
   keep the talk-start target; one fixed stderr diagnostic per sweep.
2. **Installed** — every lookup is one fresh reader call, memoized per talk ID
   within the sweep and run sequentially. Nothing is persisted.
3. **Present but incomplete or symlinked** — every lookup is unavailable, with no
   fallback route (as `ev-planning-contract.md` does for the Tesla provider).

An unavailable read HOLDS the talk's arriving leg (`meeting_source`): falling back
to the talk start would patch a correctly retargeted block back and forth on
every provider blip, the churn #334 exists to stop.

The hold ID is never derived here and never used for anything; only its start
and end are read. `prep_targets` applies the contract invariant ("the interval
ending at the talk start") twice: as a cost gate on the generic fetched events
before any call, and as a staleness check on the hold that comes back.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import event_bounds  # noqa: E402

MOUNT = Path("/home/node/.claude/skills")
SKILL_DIR = "tessl__talk-prep"
READER_PARTS = (SKILL_DIR, "scripts", "talk-prep.py")
READER = MOUNT.joinpath(*READER_PARTS)
MAX_OUTPUT = 64 * 1024
TIMEOUT = 15
EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,1024}")

# Legs that arrive at the talk: the only ones a Prep hold retargets.
ARRIVING_DIRECTIONS = frozenset({"outbound", "bridge"})

NOT_INSTALLED = (
    "[drive-engine] talk-prep is not installed in this chat (no native "
    "tessl__talk-prep mount): talk drives keep the talk-start arrival target."
)


class PrepWindowUnavailable(ValueError):
    """The Prep-window read failed: no answer, never "no hold"."""


@dataclass(frozen=True)
class PrepHold:
    """One talk's Prep hold as the reader reported it (UTC-aware instants)."""

    start: datetime
    end: datetime


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PrepWindowUnavailable("Duplicate talk-prep JSON field.")
        result[key] = value
    return result


def run_reader(argv: list[str]) -> object:
    """Run the reader with both pipes and the wall clock bounded; return its JSON."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        assert process.stdout is not None and process.stderr is not None
        output = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
        deadline = time.monotonic() + TIMEOUT
        try:
            with selectors.DefaultSelector() as selector:
                for descriptor in output:
                    selector.register(descriptor, selectors.EVENT_READ)
                while selector.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise PrepWindowUnavailable("talk-prep read exceeded its bounded wait.")
                    for key, _ in selector.select(min(left, 1)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        data = output[key.fd]
                        data.extend(chunk)
                        if len(data) > MAX_OUTPUT:
                            raise PrepWindowUnavailable("talk-prep output exceeded its bound.")
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise PrepWindowUnavailable("talk-prep reader exited nonzero.")
        except (OSError, subprocess.TimeoutExpired, PrepWindowUnavailable) as exc:
            # Only this adapter's own read-only child is ever signalled.
            process.kill()
            process.wait()
            reason = str(exc) if isinstance(exc, PrepWindowUnavailable) else "no result"
            raise PrepWindowUnavailable(reason) from None
        return parse_json(bytes(output[process.stdout.fileno()]))


def parse_json(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise PrepWindowUnavailable("talk-prep reader returned invalid JSON.") from None


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise PrepWindowUnavailable("talk-prep hold time is not a string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PrepWindowUnavailable("talk-prep hold time is not RFC 3339.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PrepWindowUnavailable("talk-prep hold time has no offset.")
    return parsed


def parse_hold(value: object) -> PrepHold | None:
    """Validate the reader's whole answer; anything off-contract is unavailable."""
    if not isinstance(value, dict) or set(value) != {"hold"}:
        raise PrepWindowUnavailable("talk-prep answer is not exactly {hold}.")
    hold = value["hold"]
    if hold is None:
        return None
    if not isinstance(hold, dict) or set(hold) != {"id", "start", "end"}:
        raise PrepWindowUnavailable("talk-prep hold is not exactly {id, start, end}.")
    if not isinstance(hold["id"], str) or not hold["id"]:
        raise PrepWindowUnavailable("talk-prep hold has no id.")
    start, end = _instant(hold["start"]), _instant(hold["end"])
    if not start < end:
        raise PrepWindowUnavailable("talk-prep hold does not end after it starts.")
    return PrepHold(start=start, end=end)


def reader_path(mount: Path = MOUNT) -> Path | None:
    """The validated reader, or None when talk-prep is not installed at all.

    Raises `PrepWindowUnavailable` for a present but incomplete or symlinked
    install. Never resolves a symlink: each component is `lstat`-checked.
    """
    if not os.path.lexists(mount / SKILL_DIR):
        return None
    try:
        if not stat.S_ISDIR(mount.lstat().st_mode):
            raise PrepWindowUnavailable("Reinstall the complete regular talk-prep skill.")
        candidate = mount
        for index, part in enumerate(READER_PARTS):
            candidate = candidate / part
            mode = candidate.lstat().st_mode
            expected = stat.S_ISREG if index == len(READER_PARTS) - 1 else stat.S_ISDIR
            if not expected(mode):
                raise PrepWindowUnavailable("Reinstall the complete regular talk-prep skill.")
    except OSError:
        raise PrepWindowUnavailable("Reinstall the complete regular talk-prep skill.") from None
    return candidate


_UNRESOLVED = object()


@dataclass
class PrepWindows:
    """One sweep's Prep-window lookups: install resolved lazily, answers memoized.

    `run` and `mount` are injected by tests, the way `TeslaPlanning(run=...)` is.
    Nothing touches the filesystem until the first lookup, so a sweep with no
    gate candidate never spawns, stats or prints anything.
    """

    run: Callable[[list[str]], object] = run_reader
    mount: Path = MOUNT
    diagnostics: list[str] = field(default_factory=list)
    _reader: object = field(default=_UNRESOLVED, repr=False)
    _answers: dict[str, PrepHold | None | PrepWindowUnavailable] = field(
        default_factory=dict, repr=False
    )

    def note(self, message: str) -> None:
        """One stderr line per distinct diagnostic per sweep."""
        if message not in self.diagnostics:
            self.diagnostics.append(message)
            print(message, file=sys.stderr)

    def _resolve(self) -> Path | None:
        if self._reader is _UNRESOLVED:
            try:
                self._reader = reader_path(self.mount)
            except PrepWindowUnavailable as exc:
                self._reader = exc
        if isinstance(self._reader, PrepWindowUnavailable):
            raise self._reader
        if self._reader is None:
            self.note(NOT_INSTALLED)
            return None
        assert isinstance(self._reader, Path)
        return self._reader

    def lookup(self, event_id: str) -> PrepHold | None:
        """The talk's Prep hold, None for none, or `PrepWindowUnavailable`."""
        if not EVENT_ID.fullmatch(event_id):
            return None  # the provider would refuse it: no call, no hold
        if event_id not in self._answers:
            try:
                reader = self._resolve()
                self._answers[event_id] = (
                    None
                    if reader is None
                    else parse_hold(
                        self.run([sys.executable, str(reader), "--prep-window", event_id])
                    )
                )
            except (OSError, PrepWindowUnavailable) as exc:
                failure = (
                    exc
                    if isinstance(exc, PrepWindowUnavailable)
                    else PrepWindowUnavailable("talk-prep reader could not run.")
                )
                self._answers[event_id] = failure
        answer = self._answers[event_id]
        if isinstance(answer, PrepWindowUnavailable):
            raise answer
        return answer


def prep_targets(
    meetings: Iterable, events: Iterable, windows: PrepWindows
) -> tuple[dict[str, datetime], frozenset[str]]:
    """Each talk's Prep-start arrival target, and the talks whose read failed.

    `meetings` is the scan output; only a meeting with an outbound or bridge leg
    can be retargeted. `events` is the fetched calendar window before flight
    masking (and without the engine's own drive blocks, which would otherwise
    abut every talk they serve). The reader is called only when some other
    timed, non-cancelled event ends exactly at the meeting's start: the
    interval a Prep hold is. A returned hold is used only when it still ends at
    the talk start; one that does not is stale (the talk moved and talk-prep has
    not repaired it yet), so the talk keeps today's target with a diagnostic and
    the sweep after the repair retargets it.
    """
    ends: dict[datetime, set[str]] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("status") == "cancelled":
            continue
        bounds = event_bounds(event)
        if bounds is not None:
            ends.setdefault(bounds[1], set()).add(str(event.get("id") or ""))

    arrival: dict[str, datetime] = {}
    unavailable: set[str] = set()
    for meeting in meetings:
        meeting_id = getattr(meeting, "meeting_id", None)
        start = getattr(meeting, "start", None)
        if not isinstance(meeting_id, str) or not isinstance(start, datetime):
            continue
        if not any(
            getattr(leg, "direction", None) in ARRIVING_DIRECTIONS
            for leg in getattr(meeting, "legs", ())
        ):
            continue
        if not ends.get(start, set()) - {meeting_id}:
            continue  # nothing ends at the talk start: no Prep hold to read
        try:
            hold = windows.lookup(meeting_id)
        except PrepWindowUnavailable as exc:
            unavailable.add(meeting_id)
            windows.note(
                f"[drive-engine] talk-prep Prep window unavailable for meeting "
                f"{meeting_id} ({exc}); its drive is held as it is."
            )
            continue
        if hold is None:
            continue
        if hold.end != start:
            windows.note(
                f"[drive-engine] talk-prep Prep hold for meeting {meeting_id} no "
                "longer ends at the talk start (talk moved?); arriving by the talk "
                "start until talk-prep moves it."
            )
            continue
        arrival[meeting_id] = hold.start
    return arrival, frozenset(unavailable)
