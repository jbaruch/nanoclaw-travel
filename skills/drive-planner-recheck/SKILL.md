---
name: drive-planner-recheck
description: "RETIRED — superseded by the unified drive-engine (#156). No longer polls or alerts on drive-block traffic growth (its schedule is removed). Not a user workflow; never invoke directly."
user-invocable: false
disable-model-invocation: true
---

# Drive Planner Recheck (Retired)

Background bundle — not a workflow and not an action router. It has no steps and must never be executed or invoked; do not parallelize or freelance over it.

This skill is **retired** along with drive-planner. It watched drive-planner's blocks for traffic growth and pushed leave-earlier / leave-now alerts; the unified drive-engine (#156) now owns drive blocks, so this poll is disabled (its `cadence` is removed) and it no longer runs. Traffic-growth rechecks for the unified engine, if reintroduced, will live in drive-engine.
