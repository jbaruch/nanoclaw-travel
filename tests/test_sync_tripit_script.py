"""Smoke/contract test for skills/nightly-travel-sync/scripts/sync-tripit.sh.

The wrapper has no branching logic to unit-test — it cd's into the
orchestrator-global `reclaim-tripit-timezones-sync` package and execs
the sync. This locks the contract `mcp__nanoclaw__sync_tripit()` relies
on (the script exists, is an executable valid-bash file, runs under
`set -euo pipefail`, and invokes the expected sync entrypoint) and that
it fails loudly when the package is absent rather than running `node`
in the wrong cwd.
"""

from __future__ import annotations

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


def test_script_fails_loudly_when_package_absent():
    """With the orchestrator-global package absent, the wrapper must exit
    non-zero with an actionable message — never reach `cd`/`node`."""
    if PKG_DIR.exists():
        pytest.skip("reclaim-tripit-timezones-sync is installed on this host")
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "not found" in result.stderr
