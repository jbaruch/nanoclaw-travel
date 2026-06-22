#!/usr/bin/env python3
"""Verify flight-assist credentials are present in the environment.

Reads BYAIR_MCP_URL, GOOGLE_MAPS_API_KEY, COMPOSIO_API_KEY, and
COMPOSIO_USER_ID from the environment and emits a single-line JSON
payload on stdout with boolean presence flags. The Composio credentials
gate the calendar `reconcile` capability (#55).

Info-only — always exits 0. The skill consumes the JSON and decides
what to surface to the user.
"""

import json
import os
import sys


def check_env() -> dict:
    return {
        "byair_url_present": bool(os.environ.get("BYAIR_MCP_URL")),
        "maps_key_present": bool(os.environ.get("GOOGLE_MAPS_API_KEY")),
        "composio_key_present": bool(os.environ.get("COMPOSIO_API_KEY")),
        "composio_user_present": bool(os.environ.get("COMPOSIO_USER_ID")),
    }


def main() -> int:
    print(json.dumps(check_env(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
