#!/bin/bash
# Host-op wrapper for `mcp__nanoclaw__sync_tripit()`. Runs in the
# orchestrator, where `reclaim-tripit-timezones-sync` is installed
# globally by `Dockerfile.orchestrator` (jbaruch/nanoclaw). This plugin
# is a skill bundle and can't declare that npm global itself, so the
# wrapper depends on the orchestrator providing it — and checks for it,
# failing with an actionable message instead of a bare `cd` error when
# it's absent.
#
# `set -euo pipefail` so a failed `cd` can't silently run `node` from
# the caller's cwd and feed stale state to the nightly consumer (`-e`
# guards the `cd`; the final `node` already propagates its own exit
# status as the script's). `-u` catches typo'd vars; `pipefail` is a
# safety net for any future pipe.
set -euo pipefail

PKG_DIR=/usr/local/lib/node_modules/reclaim-tripit-timezones-sync
if [ ! -d "$PKG_DIR" ]; then
    echo "sync-tripit: $PKG_DIR not found — the orchestrator image must install it (\`npm install -g reclaim-tripit-timezones-sync\` in Dockerfile.orchestrator, jbaruch/nanoclaw)." >&2
    exit 1
fi
cd "$PKG_DIR"
node sync.mjs sync --output=json
