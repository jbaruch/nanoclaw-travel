# Drive-Planner State Schema

Documents the on-disk state files the drive-planner skill reads and writes. Per `coding-policy: stateful-artifacts`.

## Owner Skill

`drive-planner`'s `skip_state.py` (now a library — the drive-planner sweep is retired, #156) owns the schema: only it migrates `schema_version`. The live writer and reader is **drive-engine**, through that owner API — its skip action (`skip_drive.py`) writes via `add_skip`, and its sweep (`reconcile_sweep.py`) reads via `load_active_skips`. No skill rewrites the file directly, so the owner's shape control is intact; no other skill reads or writes it.

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

- **Writer** — drive-engine's skip-reply path, `skip_drive.py` (the retired drive-planner `apply.py remove` no longer runs). It resolves the meeting by summary, deletes its drive blocks, and calls `add_skip(meeting_id, expires=, now=)` with the expiry derived from the latest matched block anchor (meeting end) plus a pad, so the skip lapses once the meeting is past. `clear_skip(meeting_id, now=)` undoes a skip; `prune(now)` reclaims disk. All three go through the owner (`skip_state.py`) API.
- **Reader** — drive-engine's sweep (`reconcile_sweep.py`) calls `load_active_skips(now)` and passes the result to `scan(skip_state=...)` (the meeting classifier it shares with the retired drive-planner). `scan.py` consumes the returned `{meeting_id: expiry}` mapping; it never touches the file.

Tolerance:

- A **missing** file is not an error — it is indistinguishable from "no skips yet" and reads as an empty map.
- A **present but corrupt** file (unparseable JSON, non-object root, missing/invalid `schema_version`, or a `schema_version` below the current floor) raises `SkipStateError` rather than being silently treated as "no skips" — silently resetting would resurrect every skipped meeting as a nag.
- A `schema_version` **newer** than this plugin reads as **"no usable prior state"** (an empty map) on the **read** path (`load_active_skips`), per `coding-policy: stateful-artifacts` — the reader is lagging, not awaiting migration, and an empty map is the safe, non-disruptive fallback (worst case the sweep re-asks; it never escalates work). The fix is to update the plugin to accept the new version. On the **write** path (`add_skip` / `clear_skip` / `prune`) a newer file is **refused** with `SkipStateError` — the no-prior-state fallback is read-only, and writing would downgrade the future-version file to v1 and clobber a newer writer's state.
- Malformed individual entries (non-string id or expiry, unparseable/naive expiry) are dropped, not fatal.

Migration:

- `schema_version` `1` is the initial version; no migration exists yet. A future shape change bumps the version and adds the owner-side upgrade-on-read per `coding-policy: stateful-artifacts`. A version below the current floor has no migration path (v1 is first) and is refused; a version above is treated as no-usable-prior-state until the plugin is updated to accept it.

## Calendar-as-State: Drive Blocks

A created drive block has no local record — the calendar event itself IS the state (Epic #59 §4). The recheck poll re-fetches the near-term window by a direct API call and reads each of its own blocks back off the event. There is no `blocks.json`; the only local state file is `skip-state.json` above. Owned by `block_props.py` (`build_block_args` / `build_description` write, `parse_block` reads).

All state lives in the event **`description`**. The Composio v3 toolkit this plugin shipped on exposed NO writable `extendedProperties` on any create/patch/update action, which forced the choice; the native Calendar API it now speaks (nanoclaw#638) does expose `extendedProperties.private`, but every deployed block already carries its state in the description, so moving is a migration in its own right, not a side effect of the transport swap. It carries three parts:

- the human line `Drive: <summary>`;
- the self-marker `[drive-planner:meeting=<id>:dir=<dir>]` — `scan.py` reads it to recognize the planner's own blocks (idempotency, lombot #50); pinned against `scan._MARKER_RE` by a test;
- a `<!--dp:{...}-->` JSON comment (compact, hidden in most calendar UIs) with the machine state:

| state key | meaning |
|-----------|---------|
| `v` | record schema version (currently `2`) |
| `b` | routed drive seconds captured at creation (recheck baseline) |
| `a` | arrival-anchor timestamp, ISO-8601 — the hard arrival deadline for `outbound` / `bridge`; for a `return` leg it is the leg end (informational, the poll never rechecks returns) |
| `o` / `d` | the routed leg endpoints (the poll re-routes exactly this pair) |
| `al` | comma-joined record of alerts already pushed — `growth` and/or `leave_now` — so a later poll never re-pings the same condition |

The leg `direction` and served meeting id come from the marker; the block's start/end carry the times (CREATE uses native nested `start` / `end` objects).

Writer / reader contract:

- **Writer** — the sweep creates blocks via `apply.py create` (idempotent: finds existing markers first via an events.list, never double-books). When an alert fires, the recheck poll emits a patch and the recheck SKILL.md applies it via `apply.py suppress` AFTER the send; the patch carries the full rebuilt `description` with only `al` updated (events.patch supports a partial `description` update).
- **Reader** — the recheck poll calls `parse_block(event)`; a non-block or malformed event yields `None` (never raises), so one bad event can't abort the poll. Only arrival-anchored legs (`outbound` / `bridge`) are rechecked; a `return` leg is created for visibility but not watched.

Migration (per `coding-policy: stateful-artifacts`):

- `v` `2` is the current version. `v` `1` was the original `extendedProperties.private` string-map shape — defunct: the live v3 toolkit has no writable extendedProperties, so no v1 record was ever written, and the description-based parser cannot read that shape anyway (it carries no `<!--dp:-->` comment). Bump on any further shape change to the description state JSON and add the owner-side upgrade in `parse_block`. A record stamped NEWER than this plugin supports — or carrying a non-int `v` — parses to `None` (no-usable-prior-state, the safe non-disruptive fallback). A missing `v` is treated as the current shape for back-compat.

Tolerance:

- A block whose state is missing or malformed (no marker, unparseable JSON, unparseable baseline / arrive-by, empty endpoints, unknown direction) parses to `None` and is treated as "not a block I recheck" — never raised on.
- The API fetch / create / find / patch go through `google_calendar_client` — the native Calendar REST API, brokered by OneCLI's gateway (nanoclaw#638).
