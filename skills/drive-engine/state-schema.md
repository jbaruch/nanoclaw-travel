# Drive-Engine State Schema

Documents the on-disk state files the drive-engine skill reads and writes. Per `coding-policy: stateful-artifacts`.

## Owner Skill

`drive-engine`'s `skip_state.py` owns the schema: only it migrates `schema_version`. Writer and reader are both co-bundled and go through that owner API — the skip action (`skip_drive.py`) writes via `add_skip`, and the sweep (`reconcile_sweep.py`) reads via `load_active_skips`. No skill rewrites the file directly, so the owner's shape control is intact; no other skill reads or writes it.

The module came from the retired `drive-planner` (#156), whose bundle was folded into drive-engine once drive-engine was its only importer (#181).

## State Directory

- Production: `/workspace/state/drive-planner/`
- Tests override via the `DRIVE_PLANNER_STATE_DIR` environment variable

The `drive-planner` name in both is deployed state, not a live reference — the store predates the #181 fold and renaming either would strand the skips already on disk. Rename only behind a migration.

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

- **Writer** — the skip-reply path, `skip_drive.py`. It resolves the meeting by summary, deletes its drive blocks, and calls `add_skip(meeting_id, expires=, now=)` with the expiry derived from the latest matched block anchor (meeting end) plus a pad, so the skip lapses once the meeting is past. `clear_skip(meeting_id, now=)` undoes a skip; `prune(now)` reclaims disk. All three go through the owner (`skip_state.py`) API.
- **Reader** — the sweep (`reconcile_sweep.py`) calls `load_active_skips(now)` and passes the result to `scan(skip_state=...)`. `scan.py` consumes the returned `{meeting_id: expiry}` mapping; it never touches the file.

Tolerance:

- A **missing** file is not an error — it is indistinguishable from "no skips yet" and reads as an empty map.
- A **present but corrupt** file (unparseable JSON, non-object root, missing/invalid `schema_version`, or a `schema_version` below the current floor) raises `SkipStateError` rather than being silently treated as "no skips" — silently resetting would resurrect every skipped meeting as a nag.
- A `schema_version` **newer** than this plugin is **refused** with `SkipStateError` on **both** paths — read (`load_active_skips`) and write (`add_skip` / `clear_skip` / `prune`). The fix is to upgrade the plugin to accept the new version.
  - The **read** path fails closed (#184) rather than taking `stateful-artifacts`' no-prior-state branch. An empty skip map is not inert: it drops every active skip, so the sweep re-plans each meeting the operator declined and pings them about it — the "escalates work" a no-prior-state fallback is forbidden to become, and precisely the lombot #49 nag this file exists to prevent. Raising surfaces at `reconcile_sweep`'s fail-closed boundary as a clean no-wake skip: the same whole-cycle skip the engine already takes when it cannot build a trustworthy desired set (`PlanBudgetExceeded`). No partial plan, no nag. The cost is explicit — while the file is future-versioned, no drive blocks are planned at all.
  - The **write** path additionally must not proceed because it would rewrite the future-version file as v1 and clobber a newer writer's state.
  - Reachable only via a plugin **downgrade** after a future v2 ships, or a hand-edited file: writer and reader co-ship in one bundle in one plugin, published together, so there is no cross-pipeline skew window (`coding-policy: stateful-artifacts`, Cross-Pipeline Schema Bumps).
- Malformed individual entries (non-string id or expiry, unparseable/naive expiry) are dropped, not fatal.

Migration:

- `schema_version` `1` is the initial version; no migration exists yet. A future shape change bumps the version and adds the owner-side upgrade-on-read per `coding-policy: stateful-artifacts`. A version below the current floor has no migration path (v1 is first) and is refused; a version above is refused on both paths until the plugin is upgraded to accept it (see Tolerance — this artifact fails closed rather than reading a newer file as no-usable-prior-state, #184).

## Calendar-as-State: Drive Blocks

A drive block has no local record — the calendar event itself IS the state (Epic #59 §4). The sweep re-fetches the near-term window by a direct API call and reads each block back off the event. There is no `blocks.json`; the only local state file is `skip-state.json` above.

Every block the engine writes is owned by `block_codec.py` — marker template, machine-state keys, the generations it recognizes, and its version/tolerance rules all live there as named constants and its module docstring. Per `coding-policy: script-as-black-box`, this file does not restate them.

### Where the state lives — the #178 migration (dual-read)

Machine state is moving off the human-visible event **description** into **`extendedProperties.private`**, a machine-only field. The description carried it only because the Composio v3 toolkit exposed no writable `extendedProperties`; the native Calendar API (nanoclaw#638) does, so the constraint is gone. Every deployed block still carries its state in the description, so the move is a live-data migration with a transition window, not a field swap.

Rollout order (per `coding-policy: stateful-artifacts`, Cross-Pipeline Schema Bumps — dual-accept readers ship before the writer flips):

1. **Dual-read (this step).** `block_codec.parse_block` reads `extendedProperties.private` FIRST and the description SECOND — a block written either way round-trips. `fetch_events` carries `extendedProperties` through its field projection so the reader receives it. Nothing writes the extended-properties form yet, so every live block still parses off its description and the new branch lies dormant. No deployed block is orphaned.
2. **Writer flip (follow-up).** `calendar_apply` emits `build_extended_properties` on create/patch and drops the machine state (marker + `<!--dengine:-->` comment) from the description, keeping the human line the operator sees. `scan.py` and `meeting_source.exclude_drive_block_events` — which today recognize a block by its **description** marker — must move to the extended-properties signal in the same step, or a flipped block reads as an unmanaged meeting.
3. **Age-out / drop the description reader (follow-up).** Once no description-state block remains in the near-term window, retire the description branch of `parse_block`.

Extended-properties shape (`extendedProperties.private`, a flat string→string map; `block_codec.build_extended_properties` is the source of truth): every key is `dengine_`-namespaced to stay collision-safe in the shared map, every value is a string. `dengine_schema_version` (auditable version, spelled out per `coding-policy: stateful-artifacts`), `dengine_leg` (leg identity), `dengine_kind`, `dengine_b` (baseline seconds), `dengine_a` (anchor ISO-8601), `dengine_we` (transfer window end, optional), `dengine_o` / `dengine_d` (routed endpoints), `dengine_al` (comma-joined alert record). A map whose version is missing or not the current one reads as "no unified state here" and the reader falls back to the description, mirroring the description reader's unknown-version handling. `UNIFIED_BLOCK_SCHEMA_VERSION` (the drive-engine codec's version constant) is unchanged — the state's fields and meaning are identical, only its carrier moves.

Blocks are stamped Tangerine (`colorId` "6") so they read as visually distinct from meetings and flights (#167). The colour is a write-only presentation attribute, not machine state read back off the event — `calendar_apply.py` sets it on both create and shift (named constant `_DRIVE_BLOCK_COLOR_ID`); no reader consults it.

The API fetch / create / patch / delete go through `google_calendar_client` — the native Calendar REST API, brokered by OneCLI's gateway (nanoclaw#638).

### Legacy drive-planner blocks — recognized, never written

Blocks the retired drive-planner (#156) left on the calendar carry a `[drive-planner:meeting=<id>:dir=<dir>]` marker and a `<!--dp:{...}-->` state comment. **Nothing writes this shape** — the sweep that did is retired and its codec is deleted (#181). Two readers still care, and both read the marker only:

- `scan.py` (`_MARKER_RE`) buckets the served meeting as `has_block`, so the engine does not plan a duplicate drive on top of a block that already exists;
- `block_codec.parse_block` classifies the event as `GEN_LEGACY_DP` on the marker plus the *presence* of the `<!--dp:-->` comment, so `meeting_source.exclude_drive_block_events` can keep it in the scan input while dropping the engine's own blocks.

The `<!--dp:-->` payload's keys (`v`, `b`, `a`, `o`, `d`, `al`) are **not decoded by anything** — `block_props.parse_block` was their only reader and went with #181. They are inert bytes on deployed events; the comment survives as a recognition signal, not a record.

The engine never converges or deletes these blocks (`_MANAGED_LEGACY` is empty) — the operator cleans them up. Once none remain on the calendar, both readers above are dead code and can go.
