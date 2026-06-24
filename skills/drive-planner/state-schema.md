# Drive-Planner State Schema

Documents the on-disk state files the drive-planner skill reads and writes. Per `coding-policy: stateful-artifacts`.

## Owner Skill

`drive-planner` (this tile) is the sole owner. Only this skill migrates `schema_version`. The sweep is both writer and reader; no other skill reads or writes these files.

## State Directory

- Production: `/workspace/state/drive-planner/`
- Tests override via the `DRIVE_PLANNER_STATE_DIR` environment variable

## Files

### `skip-state.json`

The user's "skip this meeting" decisions, with per-skip expiry. Owned by `skip_state.py`.

```json
{
  "schema_version": 1,
  "skips": {
    "evt_42": "2026-07-01T17:00:00-05:00"
  }
}
```

Fields:

- `schema_version` (int, required) — currently `1`
- `skips` (object, required) — map of `meeting_id` → ISO-8601 expiry timestamp (tz-aware). The skip is active while its expiry is strictly after `now`; once expired it is dropped on the next read/prune and the meeting re-enters `needs_decision`.

Writer / reader contract:

- **Writer** — the sweep calls `add_skip(meeting_id, expires=, now=)` when the user answers "skip" (expiry is the meeting's end), `clear_skip(meeting_id, now=)` to undo, and `prune(now)` to reclaim disk.
- **Reader** — the sweep calls `load_active_skips(now)` and passes the result to `scan(skip_state=...)`. `scan.py` consumes the returned `{meeting_id: expiry}` mapping; it never touches the file.

Tolerance:

- A **missing** file is not an error — it is indistinguishable from "no skips yet" and reads as an empty map.
- A **present but corrupt** file (unparseable JSON, non-object root, missing/invalid `schema_version`, or a `schema_version` below the current floor) raises `SkipStateError` rather than being silently treated as "no skips" — silently resetting would resurrect every skipped meeting as a nag.
- A `schema_version` **newer** than this tile reads as **"no usable prior state"** (an empty map) on the **read** path (`load_active_skips`), per `coding-policy: stateful-artifacts` — the reader is lagging, not awaiting migration, and an empty map is the safe, non-disruptive fallback (worst case the sweep re-asks; it never escalates work). The fix is to update the tile to accept the new version. On the **write** path (`add_skip` / `clear_skip` / `prune`) a newer file is **refused** with `SkipStateError` — the no-prior-state fallback is read-only, and writing would downgrade the future-version file to v1 and clobber a newer writer's state.
- Malformed individual entries (non-string id or expiry, unparseable/naive expiry) are dropped, not fatal.

Migration:

- `schema_version` `1` is the initial version; no migration exists yet. A future shape change bumps the version and adds the owner-side upgrade-on-read per `coding-policy: stateful-artifacts`. A version below the current floor has no migration path (v1 is first) and is refused; a version above is treated as no-usable-prior-state until the tile is updated to accept it.

## Calendar-as-State: Drive Blocks

A created drive block has no local record — the calendar event itself IS the state (Epic #59 §4). The recheck poll re-fetches the near-term window by a direct API call and reads each of its own blocks back off the event. There is no `blocks.json`; the only local state file is `skip-state.json` above. Owned by `block_props.py` (`build_block_args` writes, `parse_block` reads).

Two surfaces per block:

- **`description`** carries the human line plus the self-marker `[drive-planner:meeting=<id>:dir=<dir>]`. `scan.py` reads the marker to recognize the planner's own blocks (idempotency, lombot #50); the marker `build_block_args` emits is pinned against `scan._MARKER_RE` by a test.
- **`extendedProperties.private`** carries the machine state, string→string (Google Calendar's private-props are string-valued):

| key | meaning |
|-----|---------|
| `drive_planner_meeting` | served meeting's event id |
| `drive_planner_dir` | leg direction — `outbound` / `return` / `bridge` |
| `drive_planner_baseline_seconds` | routed drive seconds captured at creation (recheck baseline) |
| `drive_planner_arrive_by` | hard arrival deadline, ISO-8601 |
| `drive_planner_origin` / `drive_planner_destination` | the routed leg endpoints (the poll re-routes exactly this pair) |
| `drive_planner_alerted` | comma-joined record of alerts already pushed — `growth` and/or `leave_now` — so a later poll never re-pings the same condition |

Writer / reader contract:

- **Writer** — the sweep creates blocks via `apply.py create` (idempotent: finds existing markers first, never double-books). The recheck poll patches only `drive_planner_alerted` when an alert fires.
- **Reader** — the recheck poll calls `parse_block(event)`; a non-block or malformed event yields `None` (never raises), so one bad event can't abort the poll. Only arrival-anchored legs (`outbound` / `bridge`) are rechecked; a `return` leg is created for visibility but not watched.

Tolerance:

- A block whose private props are missing or malformed (no `drive_planner_meeting`, unparseable baseline / arrive-by, empty endpoints, unknown direction) parses to `None` and is treated as "not a block I recheck" — never raised on.
- Composio is mid-retirement (nanoclaw#638 → OneCLI workspace MCP); the API fetch + patch are the pieces that re-point later, same as `composio-fetch` and `fetch_events.py`.
