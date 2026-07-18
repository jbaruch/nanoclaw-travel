"""Smoke/contract test for skills/nightly-travel-sync/scripts/sync-tripit.sh.

Post-#748 the wrapper runs the sync IN-CONTAINER (agent-image global
`reclaim-tripit-timezones-sync`) instead of host-side via the deleted
`mcp__nanoclaw__sync_tripit()` host-op. Credentials are swapped at the
OneCLI gateway, so the script sends placeholders and requires the gateway
to be engaged. This locks the contract the skill's Step 1 relies on: the
script exists, is an executable valid-bash file, runs under
`set -euo pipefail`, invokes the expected sync entrypoint, exports the
gateway placeholders, fails loudly when the package is absent, and fails
loudly when `ONECLI_URL` is unset (so placeholder creds never hit the real
TripIt/Reclaim endpoints).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "nightly-travel-sync"
    / "scripts"
    / "sync-tripit.sh"
)
PKG_DIR = Path("/usr/local/lib/node_modules/reclaim-tripit-timezones-sync")


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "sync-tripit.sh must be executable"


def test_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_runs_under_strict_mode():
    assert "set -euo pipefail" in SCRIPT.read_text()


def test_script_invokes_sync_entrypoint():
    assert "node sync.mjs sync --output=json" in SCRIPT.read_text()


def test_script_exports_gateway_placeholders():
    """Under gateway mode the CLI still requires TRIPIT_ICAL_URL and
    RECLAIM_API_TOKEN to be present; the wrapper supplies non-secret
    placeholders that the gateway swaps for the vaulted values."""
    text = SCRIPT.read_text()
    assert "export TRIPIT_ICAL_URL=" in text
    assert "export RECLAIM_API_TOKEN=" in text


def test_script_fails_loudly_when_package_absent():
    """With the agent-image-global package absent, the wrapper must exit
    non-zero with an actionable message — never reach `cd`/`node`."""
    if PKG_DIR.exists():
        pytest.skip("reclaim-tripit-timezones-sync is installed on this host")
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_script_requires_onecli_url_when_package_present():
    """With the package present but ONECLI_URL unset, the wrapper must exit
    non-zero naming ONECLI_URL rather than sending placeholder credentials to
    the real TripIt/Reclaim endpoints."""
    if not PKG_DIR.exists():
        pytest.skip("reclaim-tripit-timezones-sync is not installed on this host")
    env = {k: v for k, v in os.environ.items() if k != "ONECLI_URL"}
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "ONECLI_URL" in result.stderr
