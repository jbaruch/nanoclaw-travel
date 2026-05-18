---
name: flight-assist
description: Diagnose the flight-assist tile's environment. Verify byAir and Google Maps credentials are present. Triggers - "check flight-assist env", "diagnose flight-assist", "verify flight-assist setup", "is flight-assist ready", "flight-assist diagnostic".
---

# Flight Assist

Process steps in order. Do not skip ahead.

## Step 1 — Run the env diagnostic

Run the env-presence check:

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/check-env.py
```

The script reads `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`, prints single-line JSON on stdout, exits 0. Proceed immediately to Step 2.

## Step 2 — Report the result

Parse the JSON payload. Boolean fields:

- `byair_url_present` — `BYAIR_MCP_URL` is set
- `maps_key_present` — `GOOGLE_MAPS_API_KEY` is set

Both `true`: emit `flight-assist credentials present`. Finish here.

Either `false`: emit one line per missing variable with the fix:

- `BYAIR_MCP_URL missing. Add personal MCP link from https://byairapp.com/mcp/ to OneCLI vault and restart container.`
- `GOOGLE_MAPS_API_KEY missing. Create a Distance Matrix API key at https://console.cloud.google.com/apis/credentials and add to OneCLI vault.`

Do not invent additional diagnostics. Finish here.
