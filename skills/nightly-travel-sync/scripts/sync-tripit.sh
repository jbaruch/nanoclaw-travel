#!/bin/bash
# In-container TripIt → Reclaim timezone sync (#748).
#
# Runs `reclaim-tripit-timezones-sync` from the agent-image global install.
# Real TripIt / Reclaim / Google credentials are NOT in this container — they
# live in the OneCLI vault and are MITM-swapped at the gateway on the outbound
# requests. This script sends placeholders; ONECLI_URL / HTTPS_PROXY /
# NODE_EXTRA_CA_CERTS / ENABLE_OOO are put on the agent spawn by
# `applyOneCliToSpawn` (jbaruch/nanoclaw). It emits the CLI's single
# `--output=json` object on stdout for the skill to parse (segments →
# `mcp__nanoclaw__persist_tz_segments`; timezoneChanges / ooo / conflicts /
# errors → the chat summary).
#
# `set -euo pipefail`: -e so a failed guard/cd can't fall through to `node` in
# the wrong cwd and feed stale state to the nightly consumer; -u catches typo'd
# vars; pipefail is a safety net for any future pipe.
set -euo pipefail

PKG_DIR=/usr/local/lib/node_modules/reclaim-tripit-timezones-sync
if [ ! -d "$PKG_DIR" ]; then
    echo "sync-tripit: $PKG_DIR not found — the agent image must install it (\`npm install -g jbaruch/reclaim-tripit-timezones-sync\` in container/Dockerfile, jbaruch/nanoclaw #748)." >&2
    exit 1
fi

# Gateway required: without it the placeholder credentials below would hit the
# real TripIt / Reclaim endpoints and fail confusingly (404 / 401). Fail fast
# with the actionable cause instead — the OneCLI gateway is how the real creds
# get injected on the way out.
if [ -z "${ONECLI_URL:-}" ]; then
    echo "sync-tripit: ONECLI_URL is not set — the in-container sync needs the OneCLI gateway to inject TripIt/Reclaim/Google credentials. Confirm applyOneCliToSpawn put ONECLI_URL on the spawn and the gateway is reachable (jbaruch/nanoclaw #748)." >&2
    exit 1
fi

# OneCLI gateway placeholders — NOT secrets. The gateway swaps the real TripIt
# iCal path token (vault entry 963d7b7f, path injection) and the Reclaim Bearer
# (vault entry 12c52a66, header injection) on the outbound requests. Google OOO
# auth rides the gateway's Google connection (#638); ENABLE_OOO=1 (on the spawn)
# turns OOO on now that the GOOGLE_* vars are gone. A value already in the
# environ is honored, so a future host-set placeholder wins over this default.
export TRIPIT_ICAL_URL="${TRIPIT_ICAL_URL:-https://www.tripit.com/feed/ical/private/onecli-managed/tripit.ics}"
export RECLAIM_API_TOKEN="${RECLAIM_API_TOKEN:-onecli-managed}"

cd "$PKG_DIR"
node sync.mjs sync --output=json
