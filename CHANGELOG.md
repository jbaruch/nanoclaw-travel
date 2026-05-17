# Changelog

## Unreleased

### Initial scaffold

- **`tile.json`** — declares `jbaruch/nanoclaw-flight-assist` 0.1.0, public, with one rule (`flight-data-locality`) and one skill (`flight-assist`)
- **`rules/flight-data-locality.md`** — byAir is the single source of truth for flight data; second flight-data upstreams are forbidden by default
- **`skills/flight-assist/SKILL.md`** — minimal sequential-workflow skill with one step (`diagnose env`). Will evolve into an action router as polling, state, and event composition land in subsequent PRs
- **`skills/flight-assist/scripts/check-env.py`** — env-presence check for `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`. Emits single-line JSON; exit 0 always (info-only, not a gate)
- **CI workflows** — `test.yml` runs ruff + pytest on every PR; `publish-tile.yml` runs skill review + tile lint + `tesslio/patch-version-publish` on `main`
- **`pyproject.toml` + `requirements-dev.txt`** — pytest 8.3.4 + ruff 0.7.4, ruff scoped to `tests/` (production code lints land in a follow-up)
- **MIT license** — matches the public nanoclaw-* fleet
