# Changelog

## Unreleased

### Rules

- **Closed-loop carve-out claimed for `jbaruch/coding-policy: plugin-evals`** (2026-05-18). This tile is part of the `jbaruch/nanoclaw-*` plugin fleet — a fully-automated agent loop satisfying all three preconditions of the rule's "Narrow exception for closed-loop automated systems with no human eval-result consumption" clause: (1) no human reviews eval output for this tile in any form (no eval scores, no lift deltas, no scenario-by-scenario diffs, no regression alerts); (2) no automated gate consumes eval results (no `evals.yml` workflow, no publish-tile eval step, no downstream dashboard or paging route); (3) the owner accepts that re-introducing any consumption of eval results later — whether human review OR automated gating — requires re-introducing evals first under the standard requirement. Matches the carve-out previously claimed by `jbaruch/nanoclaw-admin` on 2026-05-09 and inherited by every `jbaruch/nanoclaw-*` tile thereafter. No `evals/` directory ships in this tile.

### Initial scaffold

- **`tile.json`** — declares `jbaruch/nanoclaw-flight-assist` 0.1.0, public, with one rule (`flight-data-locality`) and one skill (`flight-assist`)

- **`rules/flight-data-locality.md`** — byAir is the single source of truth for flight data; second flight-data upstreams are forbidden by default. The motivation behind the rule: byAir pre-computes phase logic (`computed_status`, `computed_phase_progress`, `computed_phase_risk`, `computed_phase_overdue`) and inbound-aircraft prediction (`inbound.predicted_delay`). Mixing a raw-status API would force a translation layer between two semantically-different models. The byAir Pro subscription covers every operational field this tile needs, so a second upstream would add a separate budget, a separate key, and a separate rate-limit posture for marginal data. When byAir reports "boarding" and a second API reports "scheduled", the reconciliation question has no clean answer — one upstream, one truth. Eval of byAir's MCP on 2026-05-17 confirmed all six target capabilities are addressable from byAir alone (with maps/traffic as a separate axis).

- **`skills/flight-assist/SKILL.md`** — minimal sequential-workflow skill with one step: run `check-env.py`, report missing credentials with actionable fix instructions. Will evolve into an action router as polling, state, and event composition land in subsequent PRs.

- **`skills/flight-assist/scripts/check-env.py`** — env-presence check for `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`. Emits single-line JSON; exit 0 always (info-only, not a gate).

- **`.env.example`** — documents required environment variables per `coding-policy: no-secrets`, including the deep link to the GitHub Actions secrets configuration page so a new maintainer reaches the settings page in one click.

- **CI workflows** — `test.yml` runs ruff + pytest on every PR; `publish-tile.yml` uses `jbaruch/coding-policy/.github/actions/skill-review@<sha>` (the canonical changed-skills loop) before `tessl tile lint` and `tesslio/patch-version-publish` on `main`.

- **`pyproject.toml` + `requirements-dev.txt`** — pytest 8.3.4 + ruff 0.7.4, ruff scoped to `tests/` and `skills/` per `coding-policy: code-formatting` (every shipped Python file goes through lint + format check; new skill scripts under `skills/<name>/scripts/` inherit coverage automatically).

- **MIT license** — matches the public `nanoclaw-*` fleet.

- **`.tileignore`** — excludes repo-only files (CI, tests, build artifacts, dev-time tessl-install scaffolding) from the published Tessl tile per `coding-policy: context-artifacts`.
