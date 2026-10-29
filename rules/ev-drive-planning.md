---
alwaysApply: false
applyTo: "skills/drive-engine/**, skills/flight-assist/ev_preferences.py — when planning EV drive time, selecting a vehicle or recording charging preferences"
description: Supported Tesla planning reads, literal selectors, grounded charging inputs and legacy fallback
---

# EV Drive Planning

## Required Contract

Before EV planning, preference intake or relying on scoped provider lookup, read
`skills/drive-engine/ev-planning-contract.md` in full. Resolve it within this
plugin. Missing or unreadable content stops the affected action.

## Authority

- Use only the declared Tesla provider's cached read and estimate targets.
- Planning inputs grant no vehicle, role, command, enrollment or send authority.
- Record selectors and charging evidence only from grounded owner/native inputs.
- Preserve the unchanged legacy drive plan when usable vehicle data is unavailable.
- Source publication does not prove installed producer or vehicle acceptance.
