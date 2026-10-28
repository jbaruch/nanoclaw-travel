# Conference attendance planner input

Owner: `jbaruch/nanoclaw-travel` drive-engine, module `conference_attendance.py`. Fixed artifact: `/workspace/state/drive-planner/conference-attendance.json`. `DRIVE_PLANNER_STATE_DIR` is the existing development/test override. No other skill writes this artifact directly.

## Grounding and intake

The owner or existing travel intake selects actual attendance dates, IANA zone and exact conference/venue location labels from owner-confirmed context and native trip/calendar evidence. A trip city, a CFP opportunity, a matching summary or an inferred conference name is insufficient. This is planning data, with no credential, provider or approval authority. Recording a row performs no calendar/provider operation and proves neither physical attendance nor native attestation.

Invoke the co-shipped owner from the installed skill directory:

```bash
python3 /home/node/.claude/skills/tessl__drive-engine/record_attendance.py < /tmp/attendance.json
```

The checkout equivalent is `python3 skills/drive-engine/record_attendance.py`; no other arguments are accepted. The input is one schema-1 envelope with exactly one attendance:

```json
{
  "schema_version": 1,
  "attendances": [{
    "id": "example-conference-2020",
    "starts_on": "2020-07-13",
    "ends_on": "2020-07-14",
    "time_zone": "America/Denver",
    "venue_locations": ["ExampleConf Denver", "Conference Center Address"]
  }]
}
```

All fields are required and unknown fields are refused. `id` is a stable owner-selected occurrence identifier, at most 300 characters, not a fabricated calendar ID. Dates are canonical ISO dates, inclusive, with start no later than end; `9999-12-31` is refused to keep the exclusive end boundary representable. The timezone must resolve through `zoneinfo`. Labels are bounded literal strings, at most 1,000 characters each, with no controls or invalid Unicode; normalized duplicates are refused. At most 64 labels fit one row. An empty label list explicitly withdraws that occurrence's matching without deleting its record. Never select a city-only label; a city never proves conference attendance.

The writer upserts that ID, preserving every other row. Reuse the same ID to correct dates/labels or withdraw a row. Writes hold the separate `.conference-attendance.lock`, validate prior state, write a private temporary file, fsync and atomically replace. New state/lock files have mode 0600. The full stored envelope carries the same schema and an array of at most 128 uniquely identified rows, bounded to 256 KiB. Missing state starts empty. Duplicate JSON keys, corrupt/unknown schemas, unreadable/nonregular/symlinked state or invalid rows stop read and write; no fallback resets the file. Version 1 is initial, with no older migration. A later owner version must supply its migration before accepting older input.

Success stdout is `{"schema_version":1,"recorded":true,"id":"..."}`, exit 0. Failure stdout is schema 1 with `recorded:false` and a fixed repair diagnostic, plus stderr and nonzero exit. The CLI accepts bounded UTF-8 JSON stdin only and never echoes labels or source data on failure.

## Sweep matching

The sweep reads the optional store through its owner. Missing state makes no attendance claim; malformed or future-version state fails the sweep closed before calendar writes. Readers never migrate or write. Stored data is not automatically populated from untrusted calendar text.

A meeting must retain its nonempty string ID from the native Calendar projection and have a complete timed interval. Both boundaries must fall inside the recorded local dates: from the first date's midnight through the midnight after the last date. An end exactly at that final midnight is allowed. No summary matching is used.

Whitespace runs collapse to one space, edges are trimmed, and comparison case-folds, matching existing venue equality. The complete location must equal a listed label, or a component separated by the exact spaced delimiter ` - ` must equal it. Thus `New Booth - ExampleConf Denver` matches the approved `ExampleConf Denver` label without registering that meeting's ID. `Denver`, `OtherConf Denver`, `ExampleConf Denver Extra`, URLs and unlisted venue aliases do not match that label. No prefix, substring, fuzzy, city or geocode inference is allowed. More than one matching occurrence is ambiguous and stops the sweep for owner repair.

An on-site meeting has no anchor outbound/return drive, with or without lodging. It remains a physical neighbor: an actual bridge from/to an outside appointment survives; two recorded on-site booths of the same occurrence need no bridge. The post-arrival composer cannot regenerate suppressed attendance legs. Declined, cancelled, virtual, flight, past and skip rules retain their existing precedence. An attendance record alone never enables a flight-overlapping drive.

Source tests use synthetic records and fake Calendar/Maps. Installation and grounding of the actual occurrence, followed by a native sweep, remain runtime acceptance; source approval does not claim those happened.
