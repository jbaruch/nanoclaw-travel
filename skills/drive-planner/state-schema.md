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

- **Writer** — the sweep calls `add_skip(meeting_id, expires=, now=)` when the user answers "skip" (expiry is the meeting's end, since a skip is meaningless once the meeting is over), `clear_skip(meeting_id, now=)` to undo, and `prune(now)` to reclaim disk.
- **Reader** — the sweep calls `load_active_skips(now)` and passes the result to `scan(skip_state=...)`. `scan.py` consumes the returned `{meeting_id: expiry}` mapping; it never touches the file.

Tolerance:

- A **missing** file is not an error — it is indistinguishable from "no skips yet" and reads as an empty map.
- A **present but corrupt** file (unparseable JSON, non-object root, missing/invalid `schema_version`, or a `schema_version` newer than this tile) raises `SkipStateError` rather than being silently treated as "no skips" — silently resetting would resurrect every skipped meeting as a nag.
- Malformed individual entries (non-string id or expiry, unparseable/naive expiry) are dropped, not fatal.

Migration:

- `schema_version` `1` is the initial version; no migration exists yet. A future shape change bumps the version and adds the owner-side upgrade-on-read per `coding-policy: stateful-artifacts`.
