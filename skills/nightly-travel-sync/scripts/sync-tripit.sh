#!/bin/bash
# Required by host-agent policy: fail loudly instead of silently
# continuing past the cd or the node invocation on error. Without
# `-e`, a failed `cd` (missing package, wrong permissions) would run
# `node sync.mjs` in whatever the caller's cwd was, and a failed node
# run would still exit 0 via the bash default last-command-wins
# semantics. Either path silently produced stale state for the nightly
# consumer. `-u` catches typo'd vars; `pipefail` matters if a future
# edit adds a pipe (no pipes yet, but it's one safety net to carry
# forward).
set -euo pipefail

cd /usr/local/lib/node_modules/reclaim-tripit-timezones-sync
node sync.mjs sync --output=json
