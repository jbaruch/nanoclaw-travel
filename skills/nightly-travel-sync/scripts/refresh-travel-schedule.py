import json
import os
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent.parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

import tripit_feed  # noqa: E402 — shared library resolves after the mount-path setup.

SCHEMA_VERSION = tripit_feed.SCHEMA_VERSION
TIMEOUT_SECONDS = tripit_feed.TIMEOUT_SECONDS

URL_PATH = "/workspace/group/tripit-url.txt"
OUTPUT_PATH = "/workspace/group/travel-schedule.json"


def fetch_schedule(*, now=None):
    """Read the feed; main alone writes the schedule cache."""
    return tripit_feed.fetch_schedule(
        now=now if now is not None else datetime.now(UTC),
        url_path=URL_PATH,
        timeout_seconds=TIMEOUT_SECONDS,
    )


def write_schedule(events):
    """Validate the actual staged bytes and retain the previous cache on failure."""
    try:
        descriptor, tmp_path = tempfile.mkstemp(
            prefix=Path(OUTPUT_PATH).name + ".",
            suffix=".tmp",
            dir=Path(OUTPUT_PATH).parent,
        )
    except OSError as exc:
        raise RuntimeError(
            "Cannot stage TripIt schedule; check output permissions and disk space, "
            "then retry; previous cache retained."
        ) from exc
    try:
        try:
            with os.fdopen(descriptor, "w") as output:
                # Match a normal open(..., "w") so readers sharing the
                # group mount retain access. This standalone writer is
                # single-threaded; restore the process umask immediately.
                mask = os.umask(0)
                os.umask(mask)
                os.fchmod(output.fileno(), 0o666 & ~mask)
                json.dump(events, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            try:
                with open(tmp_path) as staged:
                    verified = json.load(staged)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "Invalid staged TripIt JSON; retry the refresh; previous cache retained."
                ) from exc
            tripit_feed.validate_schedule(verified)
            os.replace(tmp_path, OUTPUT_PATH)
            return verified
        except OSError as exc:
            raise RuntimeError(
                "Cannot replace TripIt schedule; check output permissions and disk space, "
                "then retry; previous cache retained."
            ) from exc
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass  # Successful replacement already consumed this invocation's file.


def main():
    events = fetch_schedule()
    # Atomic write per `coding-policy: stateful-artifacts`: stage a
    # same-dir `.tmp` sibling, validate it, then `os.replace` it into
    # place. A failed or partial refresh never truncates or freshens the
    # live travel-schedule.json — the mtime-preserving failure contract
    # the sibling state-schema.md documents. Mirrors build-travel-db.py.
    verified = write_schedule(events)

    # Structured JSON run summary per `jbaruch/coding-policy:
    # script-delegation` (Script Requirements: JSON-producing — output
    # structured data, not prose). The durable artifact is
    # travel-schedule.json (written above); this single stdout line is
    # the operator-facing signal in nightly logs.
    type_counts = Counter(e["type"] for e in verified)
    print(
        json.dumps(
            {
                "events_written": len(events),
                "type_breakdown": dict(type_counts),
                "sample": [
                    {"type": e["type"], "summary": e["summary"], "start": e["start"]}
                    for e in verified[:15]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    # Self-error-handling per `jbaruch/coding-policy: script-delegation`:
    # the expected operational failures — a missing or empty
    # `tripit-url.txt`, or an ICS fetch that fails both attempts — exit
    # non-zero with a single clean stderr line (not a traceback) so
    # SKILL.md Step 2 can surface it verbatim. Programmer/data-shape
    # faults (e.g. the post-write validation asserts) still propagate.
    try:
        main()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"refresh-travel-schedule: {exc}", file=sys.stderr)
        sys.exit(1)
