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
    suppress stdin {"patches": [{"event_id": "...", "calendar_id": "...",
                    "private": {<full extendedProperties.private map>}}]}
             stdout {"patched": ["<event_id>", ...]}
             Invoked by the recheck SKILL.md AFTER the alert is sent, so a
             failed send never permanently suppresses an alert.

This script is NOT a scheduler precheck — it is invoked by the agent and its
exit code is read directly: exit 0 on success, non-zero with a `{"error": ...}`
stderr line on a usage error or an unrecovered Composio failure (the agent
surfaces that to the user). stdlib-only (plus in-tile modules).
"""

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BUNDLE_DIR))

_FLIGHT_ASSIST_RUNTIME = Path("/home/node/.claude/skills/tessl__flight-assist")
_FLIGHT_ASSIST_DEV = _BUNDLE_DIR.parent / "flight-assist"


def _flight_assist_dir() -> Path:
    if _FLIGHT_ASSIST_RUNTIME.is_dir():
        return _FLIGHT_ASSIST_RUNTIME
    if _FLIGHT_ASSIST_DEV.is_dir():
        return _FLIGHT_ASSIST_DEV
    raise FileNotFoundError(
        "drive-planner apply: cannot locate the co-shipped flight-assist skill at "
        f"{_FLIGHT_ASSIST_RUNTIME} (runtime) or {_FLIGHT_ASSIST_DEV} (dev) — composio_client "
        "ships there; both skills are part of jbaruch/nanoclaw-travel"
    )


# Composio's find/create/delete surface ships in the co-located flight-assist
# bundle; add it to the path and import its client + error type (the same
# cross-bundle import reconcile.py does in-bundle), so the calendar-write
# failures can be caught by their specific type per `coding-policy:
# error-handling` rather than a bare catch-all.
sys.path.insert(0, str(_flight_assist_dir()))

from block_props import parse_block  # noqa: E402
from composio_client import ComposioClient, ComposioError  # noqa: E402
from skip_state import add_skip  # noqa: E402

# Calendar-write failures worth catching per-op: a Composio tool error or a
# transport error. A non-write bug is not in this set and propagates.
_WRITE_ERRORS = (ComposioError, urllib.error.URLError, urllib.error.HTTPError, OSError)

# Pad the find window around a block so a small clock/timezone skew between
# create and the idempotency find never hides an existing block.
_FIND_PAD = timedelta(hours=1)

# Fallback skip horizon when a remove request carries no meeting_end and no
# blocks are found to derive one from — bounds the search window and the skip
# expiry so a future recurrence is still suppressed without pinning forever.
_DEFAULT_SKIP_HORIZON = timedelta(days=30)


def _load_composio():
    """Construct the in-tile ComposioClient from env (cross-bundle path set above)."""
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

    Parses each fetched event with `parse_block` (recognition is by the block's
    `extendedProperties.private`, not the description marker) and collects the
    directions of blocks that serve `meeting_id`. "Handled" = ANY block, so a
    create for a (meeting, direction) already in this set is skipped.
    """
    directions = set()
    for event in fetched_events:
        state = parse_block(event)
        if state is not None and state.meeting_id == meeting_id:
            directions.add(state.direction)
    return directions


def _arg_direction(create_arg: object) -> str | None:
    if not isinstance(create_arg, dict):
        return None
    ext = create_arg.get("extendedProperties")
    private = ext.get("private") if isinstance(ext, dict) else None
    value = private.get("drive_planner_dir") if isinstance(private, dict) else None
    return value if isinstance(value, str) else None


def _calendar_id_of(create_args: list) -> str:
    """The calendar id from the first dict create-arg, defaulting to "primary".

    Tolerant of non-dict entries (the create loop already skips malformed args)
    so a bad first element never raises on the idempotency find.
    """
    for arg in create_args:
        if isinstance(arg, dict) and isinstance(arg.get("calendar_id"), str):
            return arg["calendar_id"]
    return "primary"


def plan_creates(meeting: dict, fetched_events: list) -> tuple[list, list]:
    """Split a meeting's create_args into (to_create, skipped_existing). Pure.

    `to_create` are the create-arg dicts whose (meeting, direction) has no
    existing marker block; `skipped_existing` are the directions already
    present (idempotent no-op, lombot #50).
    """
    present = existing_directions(fetched_events, meeting.get("meeting_id", ""))
    to_create: list = []
    skipped: list = []
    create_args = meeting.get("create_args")
    for arg in create_args if isinstance(create_args, list) else []:
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
        if not isinstance(arg, dict):
            continue
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
        meeting_id = meeting.get("meeting_id") if isinstance(meeting, dict) else None
        if not isinstance(meeting_id, str) or not meeting_id:
            # A malformed entry (no usable meeting id) is recorded, not crashed
            # — one bad item in the request must not abort the whole batch.
            failed.append(
                {"meeting_id": meeting_id, "direction": None, "error": "missing meeting_id"}
            )
            continue
        args = meeting.get("create_args")
        if not isinstance(args, list):
            args = []
        time_min, time_max = _find_window(args)
        fetched = []
        if time_min and time_max and args:
            calendar_id = _calendar_id_of(args)
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
            except _WRITE_ERRORS as exc:
                # One leg's create failing must not abort the batch — record it
                # and keep going. The next sweep retries idempotently.
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
    calendar_id = request.get("calendar_id")
    # Default a missing / non-string / empty calendar_id rather than forwarding
    # an invalid value into find/delete.
    if not isinstance(calendar_id, str) or not calendar_id:
        calendar_id = "primary"

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
        try:
            client.delete_event({"calendar_id": calendar_id, "event_id": state.event_id})
        except ComposioError as exc:
            # A 404 means the event is already gone (a concurrent delete) — an
            # idempotent success, not a failure (matches reconcile.py). Any
            # other Composio error propagates to main's handler.
            if exc.status_code != 404:
                raise
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


def _suppress_mode(request: dict, client) -> dict:
    """Persist the recheck poll's alert-suppression records — AFTER the ping.

    The recheck SKILL.md calls this only once `mcp__nanoclaw__send_message` has
    delivered the leave-earlier / leave-now alert, so a failed send never
    permanently suppresses an alert. Each patch carries the block's FULL
    `extendedProperties.private` map (the poll built it by carrying the
    existing props forward with only `drive_planner_alerted` updated) — Google
    Calendar's PATCH replaces the whole private map, so a partial map would wipe
    the block's machine state.
    """
    patched = []
    for patch in request.get("patches", []):
        if not isinstance(patch, dict):
            continue
        event_id = patch.get("event_id")
        private = patch.get("private")
        if not isinstance(event_id, str) or not event_id or not isinstance(private, dict):
            continue
        calendar_id = patch.get("calendar_id")
        if not isinstance(calendar_id, str) or not calendar_id:
            calendar_id = "primary"
        client.patch_event(
            {
                "calendar_id": calendar_id,
                "event_id": event_id,
                "extendedProperties": {"private": private},
            }
        )
        patched.append(event_id)
    return {"patched": patched}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("create", "remove", "suppress"):
        print(
            json.dumps(
                {"error": "usage: apply.py <create|remove|suppress> (request JSON on stdin)"}
            ),
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
        elif argv[1] == "remove":
            result = _remove_mode(request, client)
        else:
            result = _suppress_mode(request, client)
    except ValueError as exc:
        # Config / usage error — missing COMPOSIO_* env, or a bad remove request.
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except _WRITE_ERRORS as exc:
        # An unrecovered Composio / transport failure during find/delete —
        # surface it to the agent. A non-write bug propagates as a traceback.
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
