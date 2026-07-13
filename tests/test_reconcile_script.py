"""Tests for skills/flight-assist/scripts/reconcile.py — the CLI entry point.

The orchestration success path is covered against a fake Composio client in
`test_calendar_reconcile.py`; this exercises the script's outer contract:
missing credentials surface as a non-zero exit + safe-shape JSON, never a
raw traceback the agent can't parse.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent / "skills" / "flight-assist" / "scripts" / "reconcile.py"
)

_COMPOSIO_VARS = ("COMPOSIO_API_KEY", "COMPOSIO_USER_ID", "COMPOSIO_BASE_URL")


def _run(env_overrides: dict) -> subprocess.CompletedProcess:
    """Run reconcile.py with the Composio vars stripped, plus given overrides.

    Stripping first keeps a maintainer's real COMPOSIO_API_KEY from leaking
    into the assertions (and from making the script attempt a live call).
    """
    env = {k: v for k, v in os.environ.items() if k not in _COMPOSIO_VARS}
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def test_missing_credentials_exits_nonzero_with_safe_json():
    result = _run({})
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip())
    assert payload == {"status": "error", "error": "credentials"}


def test_missing_user_id_alone_exits_nonzero():
    result = _run({"COMPOSIO_API_KEY": "synthetic_key_value"})
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip())
    assert payload["status"] == "error"


def test_state_failure_is_not_mislabeled_as_credentials(tmp_path):
    """With credentials present, a corrupt-state failure surfaces as `state`.

    The credential catch is scoped to ComposioClient construction only — a
    StateError (or any other failure) raised inside the reconcile run must
    surface under its real cause, not be mislabeled a credentials problem.
    """
    state_dir = tmp_path / "state" / "flight-assist"
    state_dir.mkdir(parents=True)
    # schema_version above the module's current → read_config raises StateError
    # (forward incompatibility), exercised through the reconcile run.
    (state_dir / "config.json").write_text('{"schema_version": 999}')
    result = _run(
        {
            "COMPOSIO_API_KEY": "synthetic_key_value",
            "COMPOSIO_USER_ID": "synthetic_user_value",
            "FLIGHT_ASSIST_STATE_DIR": str(state_dir),
        }
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip())
    assert payload == {"status": "error", "error": "state"}


def test_airport_drive_is_retired_marker(tmp_path):
    """Airport drive blocks are retired from flight-assist (#156) — the unified
    drive-engine owns them now. The reconcile script no longer runs a drive pass;
    it emits a stable `airport_drive` marker so the SKILL's bookkeeping reader
    keeps working, and the byAir-calendar reconcile is unaffected.
    """
    state_dir = tmp_path / "state" / "flight-assist"
    state_dir.mkdir(parents=True)
    # No config.json → run_reconcile resolves no flight calendar → no_calendar.
    result = _run(
        {
            "COMPOSIO_API_KEY": "synthetic_key_value",
            "COMPOSIO_USER_ID": "synthetic_user_value",
            "FLIGHT_ASSIST_STATE_DIR": str(state_dir),
            "GOOGLE_MAPS_API_KEY": "synthetic_maps_key",
            "BYAIR_MCP_URL": "https://example.invalid/mcp/synthetic",
        }
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout.strip())  # single-line JSON, not a traceback
    assert payload["status"] == "no_calendar"
    assert payload["airport_drive"] == {"status": "retired", "engine": "drive-engine"}
