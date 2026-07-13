---
name: drive-planner
description: "RETIRED — superseded by the unified drive-engine (#156). No longer plans or writes drive blocks (its schedule is removed). Kept only as a library: its meeting-detection modules (scan.py, fetch_events.py, skip_state.py) are imported by drive-engine's reconcile sweep. Not a user workflow; never invoke directly."
user-invocable: false
disable-model-invocation: true
---

# Drive Planner (Retired)

Background library bundle — not a workflow and not an action router. It has no steps and must never be executed or invoked; do not parallelize or freelance over it.

This skill is **retired**. The unified [drive-engine](../drive-engine/SKILL.md) (#156) now plans and writes every `Drive:` block — both airport and meeting drives — from one place, so drive-planner's own sweep is disabled (its `cadence` is removed) and it no longer creates or notifies. It stays declared only because drive-engine's reconcile sweep reuses its proven meeting-detection code as a library.

## Hosted modules (imported by drive-engine)

- `scan.py` — meeting-event classification. Public surface: `scan`, `actionable`, `MeetingClass`, `TransitLeg`.
- `fetch_events.py` — the primary-calendar window fetch (`CalendarFetcher`).
- `skip_state.py` — active-skip loading (`load_active_skips`).

drive-engine's `reconcile_sweep.py` puts this bundle on `sys.path` (runtime mount `tessl__drive-planner`, dev-clone sibling fallback) and imports from it:

```python
from scan import scan
from fetch_events import CalendarFetcher
from skip_state import load_active_skips
```

The classification rules and leg logic live in those modules and their tests — do not restate them here. Any drive blocks drive-planner created before retirement remain on the calendar for the operator to remove; drive-engine does not touch them.
