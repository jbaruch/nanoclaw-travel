# Nightly Travel Sync — State Schema

Per `coding-policy: stateful-artifacts`. This skill owns one cross-invocation, cross-tile JSON artifact: `travel-schedule.json`. (The day-indexed `travel-db.json` it rebuilds in Step 4 is owned by the sibling `check-travel-bookings` skill — see that skill's `state-schema.md`.)

## `/workspace/group/travel-schedule.json`

Flat list of upcoming TripIt events, projected from the live ICS feed.

- **Owner skill:** `nightly-travel-sync` (this skill)
- **Writer:** `scripts/refresh-travel-schedule.py` (Step 2) — the sole writer
- **Readers:**
  - `tessl__check-travel-bookings/scripts/build-travel-db.py` (same tile, Step 4 rebuilds `travel-db.json` from this file)
  - `scripts/check-travel-freshness.py` (same tile, Step 3 reads **mtime only**, never the body)
  - `nanoclaw-admin`'s `morning-brief` and `check-cfps` (cross-tile via the shared `/workspace/group/` mount, reading Trip-type records for travel-conflict checks)

### Shape (schema_version 1)

A JSON array. Each element is one event record:

```json
[
  {
    "schema_version": 1,
    "summary": "Madrid trip",
    "start": "2026-06-01",
    "end": "2026-06-05",
    "location": "Madrid, ES",
    "type": "Trip",
    "uid": "trip-100@tripit.com"
  }
]
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | integer | yes | Currently `1`. Present on every record (the artifact is a bare array, with no top-level object to hold a single version). |
| `summary` | string | yes | Event title from the ICS `SUMMARY`. |
| `start` / `end` | string | yes | `YYYY-MM-DD` for date-only VEVENTs (trip wrappers). `YYYY-MM-DDTHH:MM:SSZ` for timed VEVENTs (flights, lodging check-ins, rentals). |
| `location` | string | no | ICS `LOCATION`. |
| `type` | string | yes | `Trip` (trip-level wrapper) or the item `[Type]` from the ICS DESCRIPTION (`Flight`, `Lodging`, `Rail`, `Car Rental`, …). `Unknown` when absent. |
| `uid` | string | yes | ICS `UID`. Trip wrappers lack `item-`. Items contain it. |

### Lifecycle

- **Regenerated, not accumulated** — Step 2 overwrites the whole file from the live ICS feed every run. There is no read-modify-write continuity. The writer never migrates an existing file in place. It always emits the current `SCHEMA_VERSION`.
- **Write atomicity** — the writer stages a same-dir `.tmp` sibling, validates it is a list with the required keys, then `os.replace`s it into place. A failed fetch/parse or a partial write exits non-zero and leaves the prior file (and its mtime) untouched, which Step 3's two-tier probe detects.

### Migration policy

Bump `SCHEMA_VERSION` in `refresh-travel-schedule.py` for any record-shape change. The file is fully regenerated each run. The next successful Step 2 rewrites every record at the new version. No stored old-version state exists to upgrade. Readers are non-owners per `coding-policy: stateful-artifacts`. They extract the fields they need and tolerate the per-record `schema_version` without migrating it. A reader that gates on the version treats an unfamiliar value as "no usable schedule" and falls back to its own degraded path.
