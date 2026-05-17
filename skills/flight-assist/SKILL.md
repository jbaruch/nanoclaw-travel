---
name: flight-assist
description: Diagnose the flight-assist tile's environment — verify byAir and Google Maps credentials are present. Triggers - "check flight-assist env", "diagnose flight-assist", "verify flight-assist setup", "is flight-assist ready", "flight-assist diagnostic".
---

# Flight Assist

Process steps in order. Do not skip ahead.

This is the initial v0.1.0 scaffold. The tile currently exposes one diagnostic action; flight-status polling, event composition, and time-to-leave land in subsequent versions.

## Step 1 — Run the env diagnostic

Run the env-presence check:

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/check-env.py
```

The script reads two environment variables (`BYAIR_MCP_URL`, `GOOGLE_MAPS_API_KEY`) and prints a single-line JSON payload on stdout. Exit code is always 0 — this is an info-only diagnostic, never a gate. Proceed immediately to Step 2.

## Step 2 — Report the result

Parse the JSON payload. It contains two boolean fields:

- `byair_url_present` — `BYAIR_MCP_URL` is set
- `maps_key_present` — `GOOGLE_MAPS_API_KEY` is set

If both are `true`, emit one line: `flight-assist credentials present (status checks not yet wired — v0.1.0 scaffold)`. Finish here.

If either is `false`, emit one line per missing variable, naming the variable and the fix:

- `BYAIR_MCP_URL missing — add personal MCP link from https://byairapp.com/mcp/ to OneCLI vault and restart container`
- `GOOGLE_MAPS_API_KEY missing — create a Distance Matrix API key at https://console.cloud.google.com/apis/credentials and add to OneCLI vault`

Do not invent additional diagnostics. Do not run other tools. Finish here.
