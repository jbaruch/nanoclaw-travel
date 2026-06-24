#!/usr/bin/env python3
"""Apply sweep decisions to the calendar — idempotent create, skip-removes.

The drive-planner sweep is create-first (Epic #59 §3, confirmed interaction
model): when the precheck surfaces a meeting needing a drive block, the agent
calls this script to CREATE the prepared Free block(s); when the user replies
"skip", the agent calls it to REMOVE that meeting's blocks and record a skip so
the next sweep never recreates them. Both are deterministic calendar writes, so
they live in a script (per `coding-policy: script-delegation`), mirroring
flight-assist's `reconcile.py`.

Idempotency is the load-bearing guard (lombot #50 — requiring both directions
before "handled" produced 6 duplicate blocks). Create ALWAYS finds the
meeting's existing marker blocks first and skips any (meeting, direction) that
already exists: the sweep agent and a user reply can race on the same meeting,
so assume the race and make create a no-op when the block is already there.

Composio surface (`find_events` / `create_event` / `delete_event`) ships in the
co-located flight-assist skill's `composio_client`; imported read-only via the
runtime-mount-with-dev-fallback pattern. The marker codec + skip store are
drive-planner's own (`block_props`, `skip_state`).

Modes (subcommand on argv[1]):
    create   stdin {"meetings": [{"meeting_id": "...", "create_args": [...]}]}
             stdout {"created": [...], "skipped_existing": [...], "failed": [...]}
    remove   stdin {"meeting_id": "...", "meeting_end": "<ISO>", "now": "<ISO>",
                    "calendar_id": "primary"}
             stdout {"removed": [...], "skip_recorded": true}

This script is NOT a scheduler precheck — it is invoked by the agent and its
exit code is read directly: exit 0 on success, non-zero with a `{"error": ...}`
stderr line on a usage error or an unrecovered Composio failure (the agent
surfaces that to the user). stdlib-only (plus in-tile modules).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BUNDLE_DIR))

_FLIGHT_ASSIST_RUNTIME = Path("/home/node/.claude/skills/tessl__flight-assist")
_FLIGHT_ASSIST_DEV = _BUNDLE_DIR.parent / "flight-assist"

from block_props import parse_block  # noqa: E402
from skip_state import add_skip  # noqa: E402

# Pad the find window around a block so a small clock/timezone skew between
# create and the idempotency find never hides an existing block.
_FIND_PAD = timedelta(hours=1)

# Fallback skip horizon when a remove request carries no meeting_end and no
# blocks are found to derive one from — bounds the search window and the skip
# expiry so a future recurrence is still suppressed without pinning forever.
_DEFAULT_SKIP_HORIZON = timedelta(days=30)


def _load_composio():
    """Import and construct the in-tile ComposioClient from env, cross-bundle."""
    if _FLIGHT_ASSIST_RUNTIME.is_dir():
        flight_assist_dir = _FLIGHT_ASSIST_RUNTIME
    elif _FLIGHT_ASSIST_DEV.is_dir():
        flight_assist_dir = _FLIGHT_ASSIST_DEV
    else:
        raise FileNotFoundError(
            "drive-planner apply: cannot locate the co-shipped flight-assist skill at "
            f"{_FLIGHT_ASSIST_RUNTIME} (runtime) or {_FLIGHT_ASSIST_DEV} (dev) — composio_client "
            "ships there; both skills are part of jbaruch/nanoclaw-travel"
        )
    sys.path.insert(0, str(flight_assist_dir))
    from composio_client import ComposioClient

    return ComposioClient.from_env()


def _items(data: object) -> list:
    """Pull the `items` list out of a Composio find-events response, tolerantly.

    Mirrors flight-assist's reconcile `_items`: the Google-native
    `{"items": [...]}` payload, sometimes nested under `response_data`.
    """
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if items is None:
        nested = data.get("response_data")
        items = nested.get("items") if isinstance(nested, dict) else None
    return items if isinstance(items, list) else []


def existing_directions(fetched_events: list, meeting_id: str) -> set:
    """The set of leg directions already blocked for `meeting_id` (lombot #50).

    Parses each fetched event as a potential drive block and collects the
    directions whose marker serves `meeting_id`. "Handled" = ANY marker, so a
    create for a (meeting, direction) already in this set is skipped.
    """
    directions = set()
    for event in fetched_events:
        state = parse_block(event)
        if state is not None and state.meeting_id == meeting_id:
            directions.add(state.direction)
    return directions


def _arg_direction(create_arg: dict) -> str | None:
    private = create_arg.get("extendedProperties", {}).get("private", {})
    value = private.get("drive_planner_dir")
    return value if isinstance(value, str) else None


def plan_creates(meeting: dict, fetched_events: list) -> tuple[list, list]:
    """Split a meeting's create_args into (to_create, skipped_existing). Pure.

    `to_create` are the create-arg dicts whose (meeting, direction) has no
    existing marker block; `skipped_existing` are the directions already
    present (idempotent no-op, lombot #50).
    """
    present = existing_directions(fetched_events, meeting["meeting_id"])
    to_create: list = []
    skipped: list = []
    for arg in meeting.get("create_args", []):
        direction = _arg_direction(arg)
        if direction in present:
            skipped.append(direction)
        else:
            to_create.append(arg)
    return to_create, skipped


def _find_window(create_args: list) -> tuple[datetime | None, datetime | None]:
    """The padded [min start, max end] across a meeting's create_args."""
    starts, ends = [], []
    for arg in create_args:
        start = _parse_iso(arg.get("start", {}).get("dateTime"))
        end = _parse_iso(arg.get("end", {}).get("dateTime"))
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    if not starts or not ends:
        return None, None
    return min(starts) - _FIND_PAD, max(ends) + _FIND_PAD


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _create_mode(request: dict, client) -> dict:
    created, skipped_existing, failed = [], [], []
    for meeting in request.get("meetings", []):
        meeting_id = meeting.get("meeting_id")
        args = meeting.get("create_args", [])
        time_min, time_max = _find_window(args)
        fetched = []
        if time_min and time_max and args:
            calendar_id = args[0].get("calendar_id", "primary")
            fetched = _items(
                client.find_events(
                    {
                        "calendar_id": calendar_id,
                        "timeMin": time_min.isoformat(),
                        "timeMax": time_max.isoformat(),
                    }
                )
            )
        to_create, already = plan_creates(meeting, fetched)
        skipped_existing.extend({"meeting_id": meeting_id, "direction": d} for d in already)
        for arg in to_create:
            try:
                client.create_event(arg)
                created.append({"meeting_id": meeting_id, "direction": _arg_direction(arg)})
            except Exception as exc:  # noqa: BLE001 — report per-leg, keep going
                failed.append(
                    {"meeting_id": meeting_id, "direction": _arg_direction(arg), "error": str(exc)}
                )
    return {"created": created, "skipped_existing": skipped_existing, "failed": failed}


def _remove_mode(request: dict, client) -> dict:
    meeting_id = request.get("meeting_id")
    if not isinstance(meeting_id, str) or not meeting_id:
        raise ValueError("remove: `meeting_id` is required")
    now = _parse_iso(request.get("now"))
    if now is None:
        raise ValueError("remove: `now` must be a timezone-aware ISO-8601 string")
    # meeting_end is optional — the user reply that triggers a skip carries only
    # the meeting id. When absent it is derived below from the deleted blocks.
    meeting_end = _parse_iso(request.get("meeting_end"))
    calendar_id = request.get("calendar_id", "primary")

    # Search a generous window: the blocks sit near the meeting, which (when no
    # meeting_end is given) we don't yet know — bound the search by the skip
    # store's furthest reasonable horizon ahead of now.
    horizon = meeting_end + _FIND_PAD if meeting_end is not None else now + _DEFAULT_SKIP_HORIZON
    fetched = _items(
        client.find_events(
            {
                "calendar_id": calendar_id,
                "timeMin": (now - _FIND_PAD).isoformat(),
                "timeMax": horizon.isoformat(),
            }
        )
    )
    removed = []
    block_arrivals = []
    for event in fetched:
        state = parse_block(event)
        if state is None or state.meeting_id != meeting_id:
            continue
        client.delete_event({"calendar_id": calendar_id, "event_id": state.event_id})
        removed.append({"event_id": state.event_id, "direction": state.direction})
        block_arrivals.append(state.arrive_by)

    # Record the skip so the next sweep does not recreate the block. Expiry is
    # the meeting's end when given; otherwise the latest block arrive-by (a skip
    # is meaningless once the meeting is over, and scan filters it as `past`
    # after its start anyway). With no meeting_end and no blocks found, fall
    # back to a bounded horizon so a future recurrence is still suppressed.
    if meeting_end is not None:
        expires = meeting_end
    elif block_arrivals:
        expires = max(block_arrivals)
    else:
        expires = now + _DEFAULT_SKIP_HORIZON
    add_skip(meeting_id, expires=expires, now=now)
    return {"removed": removed, "skip_recorded": True}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("create", "remove"):
        print(
            json.dumps({"error": "usage: apply.py <create|remove> (request JSON on stdin)"}),
            file=sys.stderr,
        )
        return 2
    try:
        request = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON on stdin: {exc}"}), file=sys.stderr)
        return 2
    if not isinstance(request, dict):
        print(json.dumps({"error": "stdin must be a JSON object"}), file=sys.stderr)
        return 2

    try:
        client = _load_composio()
        if argv[1] == "create":
            result = _create_mode(request, client)
        else:
            result = _remove_mode(request, client)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — surface an unrecovered failure to the agent
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
