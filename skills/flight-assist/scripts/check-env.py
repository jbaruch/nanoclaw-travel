#!/usr/bin/env python3
"""Verify flight-assist credentials are present in the environment.

Reads BYAIR_MCP_URL and GOOGLE_MAPS_API_KEY from the environment and emits a
single-line JSON payload on stdout with boolean presence flags.

Calendar reconciliation (#55) has no flag here, on purpose. It used to report
`composio_key_present` / `composio_user_present`; #638 moved calendar access to
the native Google REST API brokered by OneCLI's gateway, which injects the
Bearer on the wire. So the container holds NO Google credential, and there is
no env var whose presence means "calendar works" — the gateway either reaches
this process or it does not, and only an actual API call can tell. Reporting a
flag for a variable that no longer exists would be a lie; synthesizing one from
HTTPS_PROXY would be a guess about orchestrator wiring this skill does not own.
`reconcile.py` is where that failure surfaces, actionably, as
`{"status": "error", "error": "gateway" | "tier"}`.

Info-only — always exits 0, makes no network call. The skill consumes the JSON
and decides what to surface to the user.
"""

import json
import os
import sys


def check_env() -> dict:
    return {
        "byair_url_present": bool(os.environ.get("BYAIR_MCP_URL")),
        "maps_key_present": bool(os.environ.get("GOOGLE_MAPS_API_KEY")),
    }


def main() -> int:
    print(json.dumps(check_env(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
