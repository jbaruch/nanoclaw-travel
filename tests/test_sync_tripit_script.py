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


def _run(
    pkg_dir: Path, *, onecli_url: str | None, path_prepend: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the wrapper against a controlled fake package dir. `onecli_url=None`
    leaves ONECLI_URL unset to exercise the gateway guard. `path_prepend` puts a
    shim dir at the front of PATH (used to stub `node`)."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    env["SYNC_TRIPIT_PKG_DIR"] = str(pkg_dir)
    if onecli_url is not None:
        env["ONECLI_URL"] = onecli_url
    if path_prepend is not None:
        env["PATH"] = f"{path_prepend}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def _make_node_shim(bin_dir: Path) -> Path:
    """A fake `node` on PATH so the happy-path test is hermetic — it asserts the
    wrapper's control flow (cd + `node sync.mjs sync --output=json`) and that the
    gateway placeholder env reached the child, without depending on a real Node
    being installed on the CI runner (the workflow provisions only Python). The
    shim echoes the args it was called with and the credential env it inherited."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    node = bin_dir / "node"
    node.write_text(
        "#!/bin/bash\n"
        'printf \'{"args":"%s","tripit":"%s","reclaim":"%s","segments":[]}\\n\''
        ' "$*" "$TRIPIT_ICAL_URL" "$RECLAIM_API_TOKEN"\n'
    )
    node.chmod(0o755)
    return bin_dir


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
    result = _run(tmp_path, onecli_url=None)  # tmp_path exists → passes PKG_DIR
    assert result.returncode != 0
    assert "ONECLI_URL" in result.stderr
    assert result.stdout == ""  # never reached `node sync.mjs`


def test_script_runs_sync_with_placeholder_defaults_when_gateway_engaged(tmp_path):
    """With the package present and the gateway engaged, the wrapper cd's in and
    runs the sync entrypoint with the gateway placeholder defaults in the child
    environment. Hermetic: `node` is shimmed on PATH, so the test asserts the
    wrapper's control flow and env without a real Node on the runner."""
    shim = _make_node_shim(tmp_path / "bin")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    result = _run(pkg, onecli_url="http://gateway.test", path_prepend=shim)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == "sync.mjs sync --output=json"  # ran the entrypoint
    assert data["tripit"].startswith("https://www.tripit.com/feed/ical/private/")
    assert data["reclaim"], "RECLAIM_API_TOKEN placeholder must be non-empty"
