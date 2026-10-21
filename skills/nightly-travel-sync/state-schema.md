# Nightly Travel Sync — State Schema

Per `coding-policy: stateful-artifacts`. This skill owns two cross-invocation JSON artifacts: `travel-schedule.json` (cross-plugin) and `travel-trips-seen.json` (the new-trip-detection snapshot, this skill only). (The day-indexed `travel-db.json` it rebuilds in Step 4 is owned by the sibling `check-travel-bookings` skill — see that skill's `state-schema.md`.)

## `/workspace/group/travel-schedule.json`

Flat list of upcoming TripIt events, projected from the live ICS feed.

- **Owner skill:** `nightly-travel-sync` (this skill)
- **Writer:** `scripts/refresh-travel-schedule.py` (Step 2) — the sole writer
- **Readers:**
  - `nanoclaw-admin` `ask-my-data` (external, read-only): projects the common required fields, validates dates, deduplicates UIDs and reports incomplete coverage. It requires v5 except during the initial rollout before `2026-10-28T00:00:00Z`, when integer versions 1–5 are accepted. After expiry, older records provide no usable prior state and require an owner refresh. Unknown or unversioned rows are unusable. This reader never migrates or writes the artifact. Its freshness contract is the owning seven-day mtime helper; missing or stale data cannot prove absence or historical totals. The installed rule is `rules/travel-schedule-contract.md`.
  - `tessl__check-travel-bookings/scripts/build-travel-db.py` (same plugin, Step 4 rebuilds `travel-db.json` from this file)
  - `scripts/check-travel-freshness.py` (same plugin, Step 3 reads **mtime only**, never the body)
  - `nanoclaw-admin`'s `morning-brief` and `check-cfps` (cross-plugin via the shared `/workspace/group/` mount, reading Trip-type records for travel-conflict checks)

### Shape (schema_version 5)

A JSON array. Each element is one event record:

```json
[
  {
    "schema_version": 5,
    "summary": "WN1683 SFO to BNA",
    "start": "2026-08-23T06:05:00Z",
    "end": "2026-08-23T10:30:00Z",
    "start_local": "2026-08-22T23:05:00-07:00",
    "end_local": "2026-08-23T05:30:00-05:00",
    "location": "San Francisco (SFO)",
    "type": "Flight",
    "uid": "item-f42d5244@tripit.com",
    "description": "..."
  }
]
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | integer | yes | Currently `5`. Present on every record (the artifact is a bare array, with no top-level object to hold a single version). |
| `summary` | string | yes | Event title from the ICS `SUMMARY`, RFC 5545 TEXT escapes decoded (**since v5**): `Check-in: Radisson Blu Airport Hotel, Oslo`, never `...Hotel\, Oslo`. Operator-visible — the booking brief and drive-engine diagnostics print it as-is (#278). |
| `start` / `end` | string | yes | `YYYY-MM-DD` for date-only VEVENTs (trip wrappers). `YYYY-MM-DDTHH:MM:SSZ` for timed VEVENTs (flights, lodging check-ins, rentals). Always UTC — the feed emits no `TZID`. |
| `start_local` / `end_local` | string | no | The same instants on the traveller's own clock, `YYYY-MM-DDTHH:MM:SS±HH:MM` (**added in v3**). Timed records only, and each half only when its OWN printed clock resolved — an arrival carries the destination's offset, and neither half ever inherits the other's. A single-location record (lodging, car rental) gets a `start_local` and no `end_local`. Reconstruction and its refusal conditions live in `scripts/tripit_local_time.py` — do not restate them here. |
| `location` | string | no | ICS `LOCATION`, RFC 5545 TEXT escapes decoded (**since v4**): `San Francisco, CA`, never `San Francisco\, CA`. Readers hand this string to a geocoder as a drivable address. `\n` / `\N` decode to a newline, so a multi-line venue address arrives as real lines. |
| `type` | string | yes | `Trip` (trip-level wrapper) or the item `[Type]` from the ICS DESCRIPTION (`Flight`, `Lodging`, `Rail`, `Car Rental`, …). `Unknown` when absent. |
| `uid` | string | yes | ICS `UID`. Trip wrappers lack `item-`. Items contain it. |
| `description` | string | no | The ICS `DESCRIPTION` verbatim (**added in v2**). Carries the `[Type] <DEP> to <ARR>` line drive-engine's TripIt-union parser reads for a Flight's route (#156 R2). Additive — readers that don't use it are unaffected. |

Any consumer reducing a timed record to a calendar date reads `start_local` / `end_local` first and falls back to the UTC field. A record without them is not an error — it is a record whose local clock the feed did not print readably.

### v1 → v2

Additive: the record gains the optional `description` field. No stored state to migrate — the schedule is regenerated in full each run, so the next Step 2 emits v2 records. Cross-plugin readers that gate on the version must accept v2: `travel-core/trip_origin.py` bumped its `SCHEDULE_SCHEMA_VERSION` to `2` (it does not read `description`, so it reads v1 and v2 identically).

### v2 → v3

Additive: the record gains the optional `start_local` / `end_local` fields (#268). No stored state to migrate — the schedule is regenerated in full each run, so the next Step 2 emits v3 records. `travel-core/trip_origin.py` bumped its `SCHEDULE_SCHEMA_VERSION` to `3`; it reads instants rather than dates and does not read the local fields, so v2 and v3 read identically there. The gate accepts every version at or below its constant, so v2 records on disk during the rollout window still read.

### v4 → v5

Value-shape: `summary` arrives with its RFC 5545 escapes decoded (#278), matching what v4 did for `location`. No stored state to migrate — the schedule is regenerated in full each run, so the next Step 2 emits v5 records. `travel-core/trip_origin.py` bumped its `SCHEDULE_SCHEMA_VERSION` to `5`; both ship in this plugin and update together.

Decoding at the writer is safe for every key derived from `summary`, and each is pinned by a test rather than assumed:

- `trip_key` slugifies via `[^a-z0-9]+`, which collapses the backslash and the comma into the same `-`. Slugs are byte-identical before and after the decode, so no stored per-trip drive-engine verdict is orphaned. This was the open question in #278 and it resolves in favour of decode-at-writer.
- `lodging_pair_key` parses a stay's check-in and check-out from summaries decoded the same way, so the two halves still pair.
- `flight_summaries` reads IATA designators, which carry no TEXT escapes to unwind.

A v4 reader parses a v5 record's fields identically — the decode removes characters no reader ever parsed on — so a version-agnostic cross-plugin reader is unaffected. The gate accepts every version at or below its constant, so v4 records on disk during the rollout window still read.

### v3 → v4

Value-shape: `location` arrives with its RFC 5545 escapes decoded (#275). No stored state to migrate — the schedule is regenerated in full each run, so the next Step 2 emits v4 records. `travel-core/trip_origin.py` bumped its `SCHEDULE_SCHEMA_VERSION` to `4`; both ship in this plugin and update together. A v3 reader parses a v4 record's fields identically — the decode removes characters no reader ever parsed — so a version-agnostic cross-plugin reader (`nanoclaw-admin`'s `morning-brief-cfp.py` `load_trips`) is unaffected either way. The gate accepts every version at or below its constant, so v3 records on disk during the rollout window still read.

### Lifecycle

- **Regenerated, not accumulated** — Step 2 overwrites the whole file from the live ICS feed every run. There is no read-modify-write continuity. The writer never migrates an existing file in place. It always emits the current `SCHEMA_VERSION`.
- **Write atomicity** — the writer stages a same-dir `.tmp` sibling, validates it is a list with the required keys, then `os.replace`s it into place. A failed fetch/parse or a partial write exits non-zero and leaves the prior file (and its mtime) untouched, which Step 3's two-tier probe detects.

### Migration policy

Bump `SCHEMA_VERSION` in `refresh-travel-schedule.py` for any record-shape change. The file is fully regenerated each run. The next successful Step 2 rewrites every record at the new version. No stored old-version state exists to upgrade. Readers are non-owners per `coding-policy: stateful-artifacts`. They extract the fields they need and tolerate the per-record `schema_version` without migrating it. A reader that gates on the version treats an unfamiliar value as "no usable schedule" and falls back to its own degraded path.

## `/workspace/group/travel-trips-seen.json`

Last-seen snapshot of the upcoming trip set, used by Step 5 to detect trips that appeared since the previous nightly run (#204). New-trip detection only — never a source of truth for trip data (that is `travel-db.json`).

- **Owner skill:** `nightly-travel-sync` (this skill)
- **Writer:** `scripts/detect-new-trips.py --commit` (Step 5) — the sole writer
- **Readers:** `scripts/detect-new-trips.py` (Step 5 detect mode) — the sole reader; no cross-plugin reader

### Shape (schema_version 1)

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-20T06:00:00Z",
  "trips": {
    "madrid-trip-2026-06": {
      "summary": "Madrid trip",
      "start": "2026-06-01",
      "end": "2026-06-05"
    }
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | integer | yes | Currently `1`. |
| `generated_at` | string | yes | UTC write time, `YYYY-MM-DDTHH:MM:SSZ`. Diagnostic only — never read back. |
| `trips` | object | yes | Map of trip `slug` → `{summary, start, end}`. Keys are the `make_slug` slugs from `build-travel-db.py`; a new key on the next run is a new trip. |

### Lifecycle

- **Snapshot, not accumulator** — each `--commit` rewrites `trips` to exactly the current upcoming set (`end` on/after today), so a trip aging into the past drops out. It is bounded by the number of upcoming trips, not ever-seen trips.
- **Seed run** — when the file is absent (or unreadable, or carries an unrecognised `schema_version`), detect mode reports `seeded: true` with an empty `new_trips`; the whole itinerary is never logged as new. The following `--commit` writes the first snapshot. Only trips appearing after the seed count as new.
- **Write ordering** — the skill logs new trips *before* `--commit`, so a logging failure leaves the snapshot untouched and the next run retries. The trusted-memory daily-log helper's line-dedup guards against a double entry across that retry.
- **Write atomicity** — same-dir `.tmp` sibling + `os.replace`, matching the other travel writers.

### Migration policy

Bump `SCHEMA_VERSION` in `detect-new-trips.py` (and this section) for any shape change. The owner is the sole reader and writer, and the snapshot is disposable — an unrecognised version reads as "no usable prior state" and triggers a seed rewrite at the current version, so no in-place migration path is needed. A lost snapshot costs at most one seed run (nothing logged that cycle).
