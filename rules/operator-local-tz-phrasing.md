---
alwaysApply: false
applyTo: "skills/flight-assist/** — when authoring, modifying or composing relative-date wording in flight-assist surfaces"
---

# Operator-Local Timezone Phrasing

## Required Read

- Before authoring, modifying or composing relative-date wording in a flight-assist surface, read `/home/node/.claude/skills/tessl__flight-assist/references/operator-local-tz-phrasing.md` in full.
- Its full text binds as rule content.
- Missing or unreadable content stops the affected action.
- Read explicitly when triggered; never `@`-import the reference.

## Boundary

- Core `current-tz` is the sole authority for the operator's current zone; never read its backing store directly.
- Never derive relative-date wording from the container clock.
- Display byAir departure and arrival clock times in their supplied airport-local offsets.
