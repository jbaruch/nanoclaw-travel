"""Behavioral contract test for skills/nightly-travel-sync/scripts/sync-tripit.sh.

Post-#748 the wrapper runs the sync IN-CONTAINER (agent-image global
`reclaim-tripit-timezones-sync`) instead of host-side via the deleted
`mcp__nanoclaw__sync_tripit()` host-op. Credentials are swapped at the
OneCLI gateway, so the script sends placeholders and requires the gateway
to be engaged.

The behavioral tests are deterministic in CI (`ci-safety: Install, Don't
Skip`): the wrapper honors `SYNC_TRIPIT_PKG_DIR`, so the tests point it at
a controlled fake package with a stub `sync.mjs` and assert what the script
actually DOES (`testing-standards: assert outcomes, not implementation`) —
it fails loudly with no package, fails fast without `ONECLI_URL`, and when
the gateway is engaged it runs the sync entrypoint with the gateway
placeholder defaults in the child environment.
"""

from __future__ import annotations

import json
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

# Vars the wrapper defaults under gateway mode — stripped from the child env so
# the tests exercise the script's own `:-default` placeholders, not whatever the
# host/CI environ happens to carry.
_STRIPPED = ("ONECLI_URL", "TRIPIT_ICAL_URL", "RECLAIM_API_TOKEN")


def _run(pkg_dir: Path, *, onecli_url: str | None) -> subprocess.CompletedProcess[str]:
    """Run the wrapper against a controlled fake package dir. `onecli_url=None`
    leaves ONECLI_URL unset to exercise the gateway guard."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    env["SYNC_TRIPIT_PKG_DIR"] = str(pkg_dir)
    if onecli_url is not None:
        env["ONECLI_URL"] = onecli_url
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def _write_stub(pkg_dir: Path) -> None:
    """A stub `sync.mjs` that echoes the credential env it received plus an empty
    segment set, so a test can assert the child process got the right values."""
    (pkg_dir / "sync.mjs").write_text(
        "console.log(JSON.stringify({"
        " tripit: process.env.TRIPIT_ICAL_URL,"
        " reclaim: process.env.RECLAIM_API_TOKEN,"
        " segments: [] }));\n"
    )


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "sync-tripit.sh must be executable"


def test_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_fails_loudly_when_package_absent(tmp_path):
    """A missing package dir must exit non-zero with an actionable message —
    never reach `cd`/`node`. Deterministic via a guaranteed-absent PKG_DIR."""
    result = _run(tmp_path / "does-not-exist", onecli_url="http://gateway.test")
    assert result.returncode != 0
    assert "not found" in result.stderr


def test_script_fails_fast_without_gateway(tmp_path):
    """Package present but ONECLI_URL unset: the wrapper must exit non-zero
    naming ONECLI_URL and never run the sync, so placeholder credentials can't
    reach the real TripIt/Reclaim endpoints."""
    _write_stub(tmp_path)
    result = _run(tmp_path, onecli_url=None)
    assert result.returncode != 0
    assert "ONECLI_URL" in result.stderr
    assert result.stdout == ""  # never reached `node sync.mjs`


def test_script_runs_sync_with_placeholder_defaults_when_gateway_engaged(tmp_path):
    """With the package present and the gateway engaged, the wrapper cd's in,
    runs the sync entrypoint, and the child process receives the gateway
    placeholder defaults for both credentials (the CLI requires them present;
    the gateway swaps them for the vaulted values)."""
    _write_stub(tmp_path)
    result = _run(tmp_path, onecli_url="http://gateway.test")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["tripit"].startswith("https://www.tripit.com/feed/ical/private/")
    assert data["reclaim"], "RECLAIM_API_TOKEN placeholder must be non-empty"
    assert data["segments"] == []
