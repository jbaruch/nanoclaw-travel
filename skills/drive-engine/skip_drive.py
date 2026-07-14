"""Skip a meeting's drive: delete its blocks now and record a skip so the sweep
won't recreate them.

The operator triggers this by replying "skip <n>" to a drive-added notification.
The agent maps the local index to the meeting NAME (never an internal id — the
message only ever showed the name) and invokes this with that summary. Resolution
is by summary, position-immune: a unique match is skipped; several meetings that
share a name are handed back as candidates for the agent to disambiguate
conversationally.

Only MEETING drives are skippable — an airport drive to/from a flight is never
offered, so this never matches one (it filters to `meeting_*` blocks).

The skip suppresses the whole meeting (both legs) via `add_skip(meeting_id)`;
the unified block's identity IS the meeting id (`meeting_source` sets
`identity=meeting.meeting_id`), which is the `skip_state` key `scan` honors.

CLI: `python3 skip_drive.py '<json-request>'` where the request is
`{"summary": "Massage"}`. The skip is timestamped by the process clock (a live
skip happens "now"), so the request carries no time field. Always prints a JSON
result to stdout (never a bare traceback — the skill parses stdout):
  {"skipped": true, "meeting": "Massage", "removed": 2}
  {"skipped": false, "unmatched": "Massage"}
  {"skipped": false, "ambiguous": "Swimming Practice", "candidates": [{"when": ...}, ...]}
  {"skipped": false, "error": "<Type>: <message>"}   # operational failure

Exit codes: 0 = a result was produced (including unmatched / ambiguous — the
script ran fine, the meeting just wasn't uniquely resolved); 1 = an operational
failure (bad Composio env, transport error, skip-store write failure) — the
`error` shape above; 2 = a caller/usage error (missing or non-JSON argument).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from block_codec import parse_block  # noqa: E402

# A skip must outlast the meeting so no sweep between now and the meeting's end
# recreates the block; once the meeting is past, `scan` filters it anyway. This
# pad over the latest leg anchor (meeting end for a round trip) covers a long
# meeting without meaningfully over-suppressing (a past meeting is filtered).
_SKIP_EXPIRY_PAD = timedelta(hours=4)
_DRIVE_SUMMARY_PREFIX = "Drive: "


def _on_path(name: str) -> None:
    runtime = Path(f"/home/node/.claude/skills/tessl__{name}")
    target = runtime if runtime.is_dir() else _BUNDLE_DIR.parent / name
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


@dataclass(frozen=True)
class SkipTarget:
    identity: str
    event_ids: tuple[str, ...]
    expires: datetime


def _event_when(event: dict) -> str | None:
    """A human local time for a candidate event, from its (offset-carrying)
    start — so the agent can tell same-named meetings apart when disambiguating."""
    start = event.get("start")
    raw = start.get("dateTime") if isinstance(start, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.strftime("%a %b %d, %H:%M")


def resolve_skip(
    events: list, *, summary: str, now: datetime
) -> tuple[SkipTarget | None, list[dict]]:
    """Find the meeting to skip by summary among current Drive blocks.

    Returns `(target, candidates)`: `target` is set only when exactly ONE meeting
    identity matches; when several same-named meetings match, `target` is None and
    `candidates` lists them (with a `when`) for the agent to disambiguate. No
    match returns `(None, [])`. Only unified `meeting_*` blocks are considered.
    """
    wanted = f"{_DRIVE_SUMMARY_PREFIX}{summary}"
    by_identity: dict[str, dict] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_summary = event.get("summary")
        if not (isinstance(event_summary, str) and event_summary.strip() == wanted):
            continue
        block = parse_block(event)
        if block is None or block.identity is None:
            continue
        if not (block.kind or "").startswith("meeting_"):
            continue
        slot = by_identity.setdefault(
            block.identity, {"event_ids": [], "anchors": [], "when": _event_when(event)}
        )
        if block.event_id:
            slot["event_ids"].append(block.event_id)
        if block.anchor is not None:
            slot["anchors"].append(block.anchor)

    if not by_identity:
        return None, []
    if len(by_identity) > 1:
        return None, [{"meeting": summary, "when": s["when"]} for s in by_identity.values()]

    identity, slot = next(iter(by_identity.items()))
    latest = max(slot["anchors"]) if slot["anchors"] else now
    return SkipTarget(identity, tuple(slot["event_ids"]), latest + _SKIP_EXPIRY_PAD), []


def _fetch_drive_events(composio, now: datetime) -> list:
    _on_path("flight-assist")
    from calendar_reconcile import _find_events_args, _items

    raw = composio.find_events(
        _find_events_args(
            calendar_id="primary",
            time_min=(now - timedelta(days=2)).isoformat(),
            time_max=(now + timedelta(days=21)).isoformat(),
        )
    )
    return _items(raw)


def skip_meeting_drive(request: dict, *, composio=None, now: datetime | None = None) -> dict:
    """Resolve + apply a skip. See module docstring for the request/result shapes."""
    summary = request.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return {"skipped": False, "error": "request must name a meeting `summary`"}
    summary = summary.strip()
    if now is None:
        now = datetime.now(timezone.utc)

    _on_path("flight-assist")
    from composio_client import ComposioClient, ComposioError

    if composio is None:
        composio = ComposioClient.from_env()

    events = _fetch_drive_events(composio, now)
    target, candidates = resolve_skip(events, summary=summary, now=now)
    if target is None:
        if candidates:
            return {"skipped": False, "ambiguous": summary, "candidates": candidates}
        return {"skipped": False, "unmatched": summary}

    # Record the skip first so a sweep that races this delete still suppresses the
    # meeting (the block reappearing for one cycle is worse than a redundant skip).
    _on_path("drive-planner")
    from skip_state import add_skip

    add_skip(target.identity, expires=target.expires, now=now)

    removed = 0
    for event_id in target.event_ids:
        try:
            composio.delete_event({"calendar_id": "primary", "event_id": event_id})
        except ComposioError as exc:
            if getattr(exc, "status_code", None) != 404:  # 404 = already gone = done
                raise
        removed += 1
    return {"skipped": True, "meeting": summary, "removed": removed}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"skipped": False, "error": "usage: skip_drive.py '<json-request>'"}))
        return 2
    try:
        request = json.loads(argv[1])
    except json.JSONDecodeError as exc:
        print(json.dumps({"skipped": False, "error": f"invalid JSON request: {exc}"}))
        return 2
    try:
        result = skip_meeting_drive(request)
    except Exception as exc:  # noqa: BLE001 — outer-boundary-process-contract
        # The skill invokes this as a subprocess and parses ONLY stdout JSON. An
        # uncaught exception (bad env from `ComposioClient.from_env`, a transport
        # error from the fetch / delete, a skip-store write failure) would emit a
        # Python traceback the skill can't read — it would look like "no result".
        # Emit the documented `{"skipped": false, "error": ...}` shape and a
        # non-zero exit so the skill reports the failure instead of hanging on
        # unparseable output. KeyboardInterrupt / SystemExit still propagate.
        print(json.dumps({"skipped": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
