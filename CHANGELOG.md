# Changelog

## Unreleased

### Initial scaffold

- **`tile.json`** — declares `jbaruch/nanoclaw-flight-assist` 0.1.0, public, with one rule (`flight-data-locality`) and one skill (`flight-assist`)

- **`rules/flight-data-locality.md`** — byAir is the single source of truth for flight data; second flight-data upstreams are forbidden by default. The motivation behind the rule: byAir pre-computes phase logic (`computed_status`, `computed_phase_progress`, `computed_phase_risk`, `computed_phase_overdue`) and inbound-aircraft prediction (`inbound.predicted_delay`). Mixing a raw-status API would force a translation layer between two semantically-different models. The byAir Pro subscription covers every operational field this tile needs, so a second upstream would add a separate budget, a separate key, and a separate rate-limit posture for marginal data. When byAir reports "boarding" and a second API reports "scheduled", the reconciliation question has no clean answer — one upstream, one truth. Eval of byAir's MCP on 2026-05-17 confirmed all six target capabilities are addressable from byAir alone (with maps/traffic as a separate axis).

- **`skills/flight-assist/SKILL.md`** — minimal sequential-workflow skill with one step: run `check-env.py`, report missing credentials with actionable fix instructions. Will evolve into an action router as polling, state, and event composition land in subsequent PRs.

- **`skills/flight-assist/scripts/check-env.py`** — env-presence check for `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`. Emits single-line JSON; exit 0 always (info-only, not a gate).

- **`.env.example`** — documents required environment variables per `coding-policy: no-secrets`, including the deep link to the GitHub Actions secrets configuration page so a new maintainer reaches the settings page in one click.

- **CI workflows** — `test.yml` runs ruff + pytest on every PR; `publish-tile.yml` uses `jbaruch/coding-policy/.github/actions/skill-review@<sha>` (the canonical changed-skills loop) before `tessl tile lint` and `tesslio/patch-version-publish` on `main`.

- **`pyproject.toml` + `requirements-dev.txt`** — pytest 8.3.4 + ruff 0.7.4, ruff scoped to `tests/` per `coding-policy: ci-safety` (production code lints in a follow-up PR matched to when production code lands).

- **MIT license** — matches the public `nanoclaw-*` fleet.
