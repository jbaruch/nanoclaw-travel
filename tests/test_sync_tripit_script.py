"""Smoke/contract test for skills/nightly-travel-sync/scripts/sync-tripit.sh.

Post-#748 the wrapper runs the sync IN-CONTAINER (agent-image global
`reclaim-tripit-timezones-sync`) instead of host-side via the deleted
`mcp__nanoclaw__sync_tripit()` host-op. Credentials are swapped at the
OneCLI gateway, so the script sends placeholders and requires the gateway
to be engaged. This locks the contract the skill's Step 1 relies on: the
script exists, is an executable valid-bash file, runs under
`set -euo pipefail`, invokes the expected sync entrypoint, exports the
gateway placeholders, fails loudly when the package is absent, fails
loudly when `ONECLI_URL` is unset (so placeholder creds never hit the real
TripIt/Reclaim endpoints), and runs the sync when the gateway is engaged.

The behavioral guard tests are deterministic in CI (`ci-safety: Install,
Don't Skip`): the wrapper honors `SYNC_TRIPIT_PKG_DIR`, so the tests point
it at a controlled fake package instead of depending on the global install
being present or absent on the host.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "nightly-travel-sync"
    / "scripts"
    / "sync-tripit.sh"
)


def _run(pkg_dir: Path, *, onecli_url: str | None) -> subprocess.CompletedProcess[str]:
    """Run the wrapper against a controlled fake package dir. `onecli_url=None`
    removes ONECLI_URL from the environment to exercise the gateway guard."""
    env = {k: v for k, v in os.environ.items() if k != "ONECLI_URL"}
    env["SYNC_TRIPIT_PKG_DIR"] = str(pkg_dir)
    if onecli_url is not None:
        env["ONECLI_URL"] = onecli_url
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


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


def test_script_declares_onecli_url_guard():
    """Static coverage for the gateway guard, complementing the behavioral
    test below: the script must test ONECLI_URL and exit before running node."""
    text = SCRIPT.read_text()
    assert '[ -z "${ONECLI_URL:-}" ]' in text
    assert "ONECLI_URL is not set" in text


def test_script_fails_loudly_when_package_absent(tmp_path):
    """A missing package dir must exit non-zero with an actionable message —
    never reach `cd`/`node`. Deterministic via a guaranteed-absent PKG_DIR."""
    result = _run(tmp_path / "does-not-exist", onecli_url="http://gateway.test")
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_script_requires_onecli_url_when_gateway_absent(tmp_path):
    """With the package present but ONECLI_URL unset, the wrapper must exit
    non-zero naming ONECLI_URL rather than sending placeholder credentials to
    the real TripIt/Reclaim endpoints."""
    result = _run(tmp_path, onecli_url=None)  # tmp_path exists → passes PKG_DIR check
    assert result.returncode != 0
    assert "ONECLI_URL" in result.stderr


def test_script_runs_sync_when_gateway_engaged(tmp_path):
    """With the package present and the gateway engaged, the guards pass and the
    wrapper cd's in and runs the sync entrypoint — exercised against a stub
    `sync.mjs` so the happy path is deterministic in CI without the real CLI."""
    (tmp_path / "sync.mjs").write_text(
        "console.log(JSON.stringify({ noChanges: true, segments: [] }));\n"
    )
    result = _run(tmp_path, onecli_url="http://gateway.test")
    assert result.returncode == 0, result.stderr
    assert '"segments"' in result.stdout
