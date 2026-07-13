---
name: drive-planner
description: "RETIRED — superseded by the unified drive-engine (#156). No longer plans or writes drive blocks (its schedule is removed). Kept only as a library: its meeting-detection modules (scan.py, fetch_events.py, skip_state.py) are imported by drive-engine's reconcile sweep. Not a user workflow; never invoke directly."
user-invocable: false
disable-model-invocation: true
---

# Drive Planner (Retired)

Background library bundle — not a workflow and not an action router. It has no steps and must never be executed or invoked; do not parallelize or freelance over it.

This skill is **retired**. The unified drive-engine (#156) now plans and writes every `Drive:` block — both airport and meeting drives — from one place, so drive-planner's own sweep is disabled (its `cadence` is removed) and it no longer creates or notifies. It stays declared only because drive-engine's reconcile sweep reuses its proven meeting-detection code as a library.

## Hosted modules (imported by drive-engine)

- `scan.py` — classifies calendar events into meetings needing a drive (virtual / declined / past / skipped / flight filtering, per-meeting anchor resolution). Public surface: `scan`, `actionable`, `MeetingClass`, `TransitLeg`.
- `fetch_events.py` — the primary-calendar window fetch (`CalendarFetcher`).
- `skip_state.py` — active-skip loading (`load_active_skips`).

The classification rules and block logic live in those modules and their tests — do not restate them here. Any drive blocks drive-planner created before retirement remain on the calendar for the operator to remove; drive-engine does not touch them.
