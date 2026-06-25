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
    list     stdin {"now": "<ISO>", "calendar_id": "primary"}
             stdout {"blocks": [{"summary": "...", "meeting_id": "...",
                    "leave_by": "<ISO>"}]} — one per meeting with a current
             drive block, ordered by leave_by. Lets the cancel UX map a user's
             ordinal / natural-language reference to the internal `meeting_id`
             without that id ever appearing in a user-facing message (#86).
    remove   stdin {"meeting_id" OR "summary": "...", "leave_by": "<ISO>",
                    "meeting_end": "<ISO>", "now": "<ISO>", "calendar_id": "..."}
             stdout {"removed": [...], "skip_recorded": true}
             `summary` resolves to the meeting_id server-side (by exact meeting
             name, position-immune) so the cancel UX never needs a raw id (#86);
             an unplannable meeting with no block resolves via the meeting event
             itself. Pass `leave_by` to pin the exact instance when meetings
             share a summary (a daily "Standup"). Non-match / still-ambiguous
             return, respectively, {"skip_recorded": false, "unmatched_summary":
             "..."} and {"skip_recorded": false, "ambiguous_summary": "...",
             "candidates": [{"summary","leave_by"}]} — never an id.
    suppress stdin {"patches": [{"event_id": "...", "calendar_id": "...",
                    "description": "<full rebuilt block description>"}]}
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

from block_props import parse_block, parse_marker  # noqa: E402
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
    """Pull the event list out of a Composio FIND_EVENT response, tolerantly.

    The live v3 `GOOGLECALENDAR_FIND_EVENT` double-nests the events at
    `data.event_data.event_data` (verified against the NAS); a flat
    `event_data`/`items` list and a `response_data` wrap are tolerated for other
    toolkit shapes. Walks one level into a dict container; first list wins.
    """
    if not isinstance(data, dict):
        return []
    for container in (data, data.get("event_data"), data.get("response_data")):
        if not isinstance(container, dict):
            continue
        for key in ("event_data", "items"):
            value = container.get(key)
            if isinstance(value, list):
                return value
    return []


def existing_directions(fetched_events: list, meeting_id: str) -> set:
    """The set of leg directions already blocked for `meeting_id` (lombot #50).

    Parses each fetched event with `parse_block` (recognition is by the block's
    description state) and collects the directions of blocks that serve
    `meeting_id`. "Handled" = ANY block, so a create for a (meeting, direction)
    already in this set is skipped.
    """
    directions = set()
    for event in fetched_events:
        state = parse_block(event)
        if state is not None and state.meeting_id == meeting_id:
            directions.add(state.direction)
    return directions


def _arg_direction(create_arg: object) -> str | None:
    """The leg direction of a create-arg, read from its description marker."""
    if not isinstance(create_arg, dict):
        return None
    marker = parse_marker(create_arg.get("description"))
    return marker[1] if marker else None


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
    """The padded [min start, max end] across a meeting's create_args.

    The v3 create contract is flat `start_datetime` + `event_duration_*`, so
    each block's end is start + its duration.
    """
    starts, ends = [], []
    for arg in create_args:
        if not isinstance(arg, dict):
            continue
        start = _parse_iso(arg.get("start_datetime"))
        if not start:
            continue
        hours = arg.get("event_duration_hour") or 0
        minutes = arg.get("event_duration_minutes") or 0
        if not isinstance(hours, int) or isinstance(hours, bool):
            hours = 0
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            minutes = 0
        starts.append(start)
        ends.append(start + timedelta(hours=hours, minutes=minutes))
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
    meetings = request.get("meetings")
    for meeting in meetings if isinstance(meetings, list) else []:
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
    summary = request.get("summary")
    has_id = isinstance(meeting_id, str) and bool(meeting_id)
    has_summary = isinstance(summary, str) and bool(summary)
    if not has_id and not has_summary:
        raise ValueError("remove: `meeting_id` or `summary` is required")
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
    # Span the window across now AND meeting_end so a late skip/remove (the
    # meeting already finished, blocks in the past) still finds the blocks, and
    # a past meeting_end never inverts the window (timeMax < timeMin). With no
    # meeting_end, look ahead a bounded horizon for the future blocks.
    if meeting_end is not None:
        time_min = min(now, meeting_end) - _FIND_PAD
        time_max = max(now, meeting_end) + _FIND_PAD
    else:
        time_min = now - _FIND_PAD
        time_max = now + _DEFAULT_SKIP_HORIZON
    fetched = _items(
        client.find_events(
            {
                "calendar_id": calendar_id,
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
            }
        )
    )

    # Resolve a name reference to its meeting_id server-side (position-immune):
    # the agent maps the user's ordinal / name onto a summary, and this finds
    # the meeting by exact summary regardless of how many other blocks exist
    # (#86). An unplannable meeting has no block, so fall back to the meeting
    # event itself, giving it a working skip path.
    if not has_id:
        assert isinstance(summary, str)  # validation above requires id or summary
        candidates = _resolve_candidates(fetched, summary)
        # Pin the exact instance when the caller passes the block's leave-by
        # (several meetings can share a summary — a daily "Standup").
        leave_by = request.get("leave_by")
        if isinstance(leave_by, str) and leave_by:
            candidates = [c for c in candidates if c[1] == leave_by]
        if not candidates:
            return {"removed": [], "skip_recorded": False, "unmatched_summary": summary}
        if len(candidates) > 1:
            # Don't guess between same-summary meetings — hand the choices back
            # (summary + leave_by, never an id) for the agent to disambiguate.
            return {
                "removed": [],
                "skip_recorded": False,
                "ambiguous_summary": summary,
                "candidates": [{"summary": summary, "leave_by": when} for _, when in candidates],
            }
        meeting_id = candidates[0][0]
    assert isinstance(meeting_id, str)  # has_id, or resolved to a single str above

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


# The block summary is "Drive: <meeting summary>" (see precheck `_leg_create_args`).
# Stripping the prefix recovers the meeting summary the operator recognizes.
_DRIVE_SUMMARY_PREFIX = "Drive: "


def _resolve_candidates(fetched: list, summary: str) -> list[tuple[str, str | None]]:
    """Meetings matching `summary` as `(meeting_id, leave_by_iso)`, position-immune.

    Prefer drive blocks serving that meeting (parsed `meeting_id` + leave-by);
    fall back to the meeting event itself (its id + start) so an `unplannable`
    meeting — which has no block — can still be skipped (#86). Matching is by
    exact summary; several distinct meetings can share a summary (a daily
    "Standup"), so the caller disambiguates by leave-by rather than this picking
    one by fetch order. De-duped by meeting_id, ordered by leave-by.
    """
    target = f"{_DRIVE_SUMMARY_PREFIX}{summary}"
    found: dict[str, str | None] = {}
    # Blocks first. A meeting has several leg blocks (outbound / return) with
    # different leave-bys; keep the EARLIEST per meeting so the value matches
    # what `list` and the notification showed (the outbound leave-by), since the
    # caller disambiguates by exactly that.
    for event in fetched:
        state = parse_block(event)
        if state is None or state.summary != target:
            continue
        leave_by = state.baseline_leave_by.isoformat()
        current = found.get(state.meeting_id)
        if current is None or leave_by < current:
            found[state.meeting_id] = leave_by
    # Meeting events too — an `unplannable` meeting has no block, and it can
    # share a name with a DIFFERENT occurrence that does (so don't stop at
    # blocks). A blocked meeting's event shares its id, so it's already covered.
    for event in fetched:
        if not isinstance(event, dict) or parse_block(event) is not None:
            continue
        if event.get("summary") == summary:
            event_id = event.get("id")
            if isinstance(event_id, str) and event_id and event_id not in found:
                start = event.get("start")
                found[event_id] = start.get("dateTime") if isinstance(start, dict) else None
    return sorted(found.items(), key=lambda item: item[1] or "")


def _list_mode(request: dict, client) -> dict:
    """List the current drive blocks, one per meeting, for the cancel UX (#86).

    Returns `{"blocks": [{summary, meeting_id, leave_by}]}` ordered by leave_by.
    The agent maps a user's ordinal ("cancel 2") or natural-language ("don't
    drive to swimming") onto a `meeting_id` from this list, so the internal id
    never has to appear in — or be typed into — a user-facing message.
    """
    now = _parse_iso(request.get("now"))
    if now is None:
        raise ValueError("list: `now` must be a timezone-aware ISO-8601 string")
    calendar_id = request.get("calendar_id")
    if not isinstance(calendar_id, str) or not calendar_id:
        calendar_id = "primary"
    fetched = _items(
        client.find_events(
            {
                "calendar_id": calendar_id,
                "timeMin": (now - _FIND_PAD).isoformat(),
                "timeMax": (now + _DEFAULT_SKIP_HORIZON).isoformat(),
            }
        )
    )
    # One entry per meeting; a meeting has several leg blocks, so key by
    # meeting_id and keep the earliest leave-by (the outbound) as the meeting's.
    by_meeting: dict[str, dict] = {}
    for event in fetched:
        state = parse_block(event)
        if state is None:
            continue
        leave_by = state.baseline_leave_by
        summary = state.summary
        if summary.startswith(_DRIVE_SUMMARY_PREFIX):
            summary = summary[len(_DRIVE_SUMMARY_PREFIX) :]
        existing = by_meeting.get(state.meeting_id)
        if existing is None or leave_by < existing["_leave_by"]:
            by_meeting[state.meeting_id] = {
                "summary": summary,
                "meeting_id": state.meeting_id,
                "leave_by": leave_by.isoformat(),
                "_leave_by": leave_by,
            }
    blocks = sorted(by_meeting.values(), key=lambda b: b["_leave_by"])
    for block in blocks:
        del block["_leave_by"]
    return {"blocks": blocks}


def _suppress_mode(request: dict, client) -> dict:
    """Persist the recheck poll's alert-suppression records — AFTER the ping.

    The recheck SKILL.md calls this only once `mcp__nanoclaw__send_message` has
    delivered the leave-earlier / leave-now alert, so a failed send never
    permanently suppresses an alert. Each patch carries the block's full new
    `description` (the poll rebuilt it with the updated alert record); since the
    machine state lives in the description and `GOOGLECALENDAR_PATCH_EVENT`
    supports a partial `description` update, the patch is just that one field.
    """
    patched = []
    patches = request.get("patches")
    for patch in patches if isinstance(patches, list) else []:
        if not isinstance(patch, dict):
            continue
        event_id = patch.get("event_id")
        description = patch.get("description")
        if not isinstance(event_id, str) or not event_id or not isinstance(description, str):
            continue
        calendar_id = patch.get("calendar_id")
        if not isinstance(calendar_id, str) or not calendar_id:
            calendar_id = "primary"
        try:
            client.patch_event(
                {
                    "calendar_id": calendar_id,
                    "event_id": event_id,
                    "description": description,
                }
            )
        except ComposioError as exc:
            # A 404 means the block was deleted concurrently — nothing to
            # suppress, an idempotent skip. One block's 404 must not fail
            # suppression for the others; any other status propagates.
            if exc.status_code != 404:
                raise
            continue
        patched.append(event_id)
    return {"patched": patched}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("create", "list", "remove", "suppress"):
        print(
            json.dumps(
                {"error": "usage: apply.py <create|list|remove|suppress> (request JSON on stdin)"}
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
        elif argv[1] == "list":
            result = _list_mode(request, client)
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
