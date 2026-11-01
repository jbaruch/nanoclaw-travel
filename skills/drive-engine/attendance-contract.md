# Conference attendance planner input

Owner: `jbaruch/nanoclaw-travel` drive-engine, module `conference_attendance.py`. Fixed artifact: `/workspace/state/drive-engine/conference-attendance.json`. `DRIVE_ENGINE_STATE_DIR` remains the development/test override. Only this owner writes or migrates it. It contains planning evidence, never credential, provider or approval authority.

## Grounding and owner choice

Select a stable owner-grounded occurrence ID, actual dates, IANA zone, exact Calendar location labels and a separately grounded **drivable venue address**. The aliases match Calendar entries; they do not select a route address. A city, a conference title, a CFP opportunity, alias ordering or a guessed booth address supplies none of those facts. Recording proves neither actual presence nor native acceptance.

For each occurrence needing hours, ask the owner which input to use: **«По расписанию»**, **«Только мои слоты»**, **«Свои часы»**. Obtain the fixed button row from the owner utility:

```bash
python3 /home/node/.claude/skills/tessl__drive-engine/record_attendance.py --choices
```

Exit 0 returns `{schema_version:1,kind:"attendance_choices",buttons:[...]}` without reading or writing attendance state. Send the question about that actual occurrence with the returned `buttons` through the existing Main owner-answer message tool. Its normal `owner.answer.v1` callback binds the actual owner's answer to the question. A typed reply to the same question is also usable. Do not manufacture a reply, use another chat's answer or treat a default as consent. If the reply context is missing, retrieve the actual question before recording.

For schedule choice, read the actual conference schedule and cite its reference for every offered day window; never derive a day from the owner's first/last Calendar talk. «Только мои слоты» records `days:[]`: no presence window and normal drives to the actual meetings. Never turn that choice into a first-to-last-slot window. Custom hours use the actual answer. Show the dates, hours, zone and route address when resolving ambiguity. The choice grants no invented hours. If there is no confirmed window, use `days:[]`: the venue remains an ordinary meeting location. A reader never populates windows from untrusted Calendar text.

## Intake and schema

Invoke the same co-shipped owner with no arguments, supplying the grounded envelope on process stdin:

```bash
python3 /home/node/.claude/skills/tessl__drive-engine/record_attendance.py
```

Checkout equivalent: `python3 skills/drive-engine/record_attendance.py`. The new intake is one schema-2 envelope with exactly one explicitly versioned row:

```json
{
  "schema_version": 2,
  "attendances": [{
    "schema_version": 2,
    "id": "example-conference-2020",
    "starts_on": "2020-07-13",
    "ends_on": "2020-07-14",
    "time_zone": "America/Denver",
    "venue_locations": ["ExampleConf Denver", "Conference Center Address"],
    "venue_location": "100 Example Street, Denver, CO",
    "days": [{
      "date": "2020-07-13",
      "onsite_from": "09:00",
      "onsite_to": "17:00",
      "source": "agenda",
      "source_ref": "https://example.test/conference/schedule"
    }]
  }]
}
```

All row and day fields are required; unknown fields are refused. `id` is an owner-selected occurrence ID, never a fabricated Calendar event ID. Inclusive occurrence dates are canonical ISO dates; `time_zone` is IANA. Each unique day lies within that span and has a same-day local `HH:MM` window. Ambiguous/nonexistent local boundaries are held for explicit correction; no DST fold, overnight window or owner hours are guessed. `source` is `agenda` or `owner`: agenda requires an actual cited HTTPS conference schedule reference, retained literally as `source_ref`; owner hours use `source_ref:null`. A syntactically accepted citation is retained input, not proof the script fetched or verified its contents.

`venue_locations` contains exact bounded literal aliases; an empty list withdraws Calendar attendance matching. `venue_location` is the independently grounded route address, not the first alias. Empty `days` withdraws window presence and commutes. The owner's parser owns the precise text/count/encoded-size limits and validation diagnostics; use `parse_attendance` and `decode_store` rather than independently approximating them.

### Readers, retained legacy rows and migration

Store 1 has exactly `schema_version:1,attendances:[...]`. Each original row has exactly `id,starts_on,ends_on,time_zone,venue_locations`. These rows keep their published all-day matching/suppression semantics until explicitly re-recorded; they receive neither windows, a canonical route address nor daily commutes by inference. Readers accept store 1 read-only and never rewrite it.

Store 2 has the same envelope keys with `schema_version:2`. Every row explicitly carries its own `schema_version`. Row 1 retains exactly the original five fields plus that discriminator; row 2 has the fields shown above. Thus an explicitly retained legacy row is distinguishable from row 2 with `days:[]`. No silent default or retag makes legacy all-day evidence an ordinary meeting, or vice versa.

The next successful owner upsert upgrades a known store 1 under the existing lock: preserve every retained row's five values in an explicit row-1 representation and upsert only the requested ID. An explicitly supplied row 2 replaces that ID's old semantics. Known legacy schema-1 intake remains supported with legacy semantics and is retained as row 1; it cannot acquire window disclosure by migration. Readers of unknown/future/damaged stores refuse before Calendar effects. The writer preserves such bytes and inode and never resets them to empty. Missing state means no attendance evidence.

Writer and sweep readers ship in this same plugin. Install the new package and retire old active readers before the first store-2 write. Published version-1 readers refuse envelope 2; that is an actionable compatibility hold, never permission to erase it or fall back to no attendance. Current readers accept both store versions. Normal cadence uses the owner's store reader, not direct edits.

## Filesystem and result contract

Before host-invoked intake, resolve runtime `HOST_UID`/`HOST_GID` and verify the container's effective user. Run as that normalized non-root agent identity. No caller UID/GID flags or identity environment overrides exist. The owner verifies held directory/lock/temporary/final descriptors, private modes, device and final identity/readback. Root execution is not acceptance evidence.

Use stdin directly. If a host runner needs a file, exclusively create mode 0600 in an owned mode-0700 directory, verify without following links and hold its descriptor through invocation. No shared predictable temporary path. Upsert retains all other rows and uses the existing separate lock, bounded private staging, fsync and atomic replacement. No second store, approval ledger or provider operation is introduced.

No-argument success stdout stays `{"schema_version":1,"recorded":true,"id":"..."}`, exit 0. Failure stdout stays schema 1, `recorded:false` and a fixed repair diagnostic, plus stderr/nonzero exit. Stderr distinguishes request decoding from state upsert and contains only fixed owner validation causes or numeric filesystem errno; no raw exception text, input values or private paths. `--choices` has the separate read-only output above. Other arguments are refused.

## Planning and reconciliation

A Calendar meeting still needs its actual nonempty native ID and complete aware timed interval. Exact normalized whole-location or spaced-dash component equality supplies label matching; no title, substring, fuzzy or city match. Row 2 additionally needs the entire interval within one recorded window; an end exactly at its upper boundary is allowed. Overlapping matching occurrences or conflicting presence windows hold for owner repair.

`attendance_source.py` supplies validated row-2 presence and occurrence scope to the sweep's one `Itinerary`, then produces daily commutes for the existing reconcile owner. Inside a window the planned position source is `attendance` at the canonical venue. Outside appointments can therefore start there even with a long gap. Return endpoints resolve at their actual end when version-2 attendance is present. Grounded on-site entries still suppress their own anchor legs; genuine venue-to-venue appointment bridges remain.

Each recorded day supplies one outbound commute arriving at its lower boundary and, when a valid home/lodging base is still available, one return departing at its upper boundary. Unavailable base/route, unknown Calendar coverage, conflicting timed appointment or protected flight interval produces a retained-leg hold. No new Calendar writer, cadence or provider route exists. Blocks use occurrence/day identities and dedicated commute kinds; they never masquerade as native meeting IDs or offer a meeting skip for a synthetic identity.

Commutes reuse the shared timeline's actual transport and attendance precedence. A window invalidated by an intervening transport cannot invent a later departure from its old venue; retain that leg for reconciliation. After charging or navigation adjusts final drive timing, a commute's home/lodging endpoint must still be available at the actual departure or arrival, and its interval must not overlap another planned drive. Exact-touch intervals do not overlap. An expired or unresolved base or conflicting final drive holds the synthetic leg without clipping its charging allowance, deleting a retained uncertain leg or changing ordinary meeting and legacy behavior.

Within an explicit row-2 occurrence, checkout ends hotel-base availability, including when `days:[]`. It does not shorten the generic trip's last-day context. When a recorded window outlasts checkout, its grounded venue remains the departure base for a later same-day airport/next leg even after the window ends. An actual new stay or transport takes precedence; missing evidence is unresolved, not a return to the released hotel or city centroid. Outside row-2 occurrence scope, shared itinerary and version-1 behavior remain unchanged.

Existing decline/cancel/virtual/past/skip filtering, flight protections, post-arrival composition, GPS rules and grounded EV planning retain their owners. Source tests use synthetic records and fake Calendar/Maps. Installed-workflow validation, the actual owner choice and any re-recording of live version-1 attendance remain separate native acceptance; no example hours approve a real occurrence.
