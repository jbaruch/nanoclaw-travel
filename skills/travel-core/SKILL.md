---
name: travel-core
description: "Shared travel-domain library bundle for the nanoclaw-travel plugin. Hosts the cross-skill Python modules trip_origin (TripIt-over-home position/anchor resolution) and airport_lead (clearance / post-arrival buffer policy) so flight-assist and the drive engine import one source of truth instead of duplicating them. Not a user workflow — background code the other skills load at runtime; never invoke directly."
user-invocable: false
disable-model-invocation: true
---

# Travel Core

Background library bundle — not a workflow and not an action router. It has no steps and must never be executed or invoked; do not parallelize or freelance over it. Its only role is to host shared code that other skills import.

It ships the travel-domain Python modules that more than one skill depends on, so there is one source of truth rather than a copy per skill. The consuming skills put this bundle on `sys.path` (runtime mount `tessl__travel-core`, dev-clone sibling fallback) and import from it.

## Hosted modules

- `trip_origin.py` — resolves the operator's planned position/anchor at a given instant (TripIt truth over the static home, #122). Public surface: `resolve_anchor`, `resolve_effective_home`, `load_travel_schedule`, and the flight-window / flight-summary helpers the meeting sweep uses. The resolution ladder itself lives in `trip_origin.py` and its tests — do not restate it here. Consumed by flight-assist (`precheck`) and the drive engine (`reconcile_sweep`).
- `airport_lead.py` — the airport clearance / post-arrival buffer policy. Public surface: `resolve_departure_clearance_minutes`, `resolve_post_arrival_minutes`, `departure_class`, `arrival_class`. The classification rules and buffer values live in `airport_lead.py` and its tests. Consumed by flight-assist (`airport_drive_inputs`) and the drive engine.

## Consumer contract

Resolve this bundle cross-bundle before importing — try the runtime mount, fall back to the dev-clone sibling, then `sys.path.insert` (the same pattern drive-engine's `fetch_events` uses to reach flight-assist's `google_calendar_client`):

```python
from pathlib import Path
import sys

_BUNDLE_DIR = Path(__file__).resolve().parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from trip_origin import resolve_anchor, resolve_effective_home  # noqa: E402
from airport_lead import resolve_departure_clearance_minutes  # noqa: E402
```

- These modules are pure library code (no I/O beyond `trip_origin`'s schedule-file read). Their behavior and tests are the source of truth; do not restate their thresholds or ladders in consumer skills — reference the module.
- Tests live in `tests/test_trip_origin.py` and `tests/test_airport_lead.py`.
