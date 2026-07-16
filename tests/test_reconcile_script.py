"""Tests for skills/flight-assist/scripts/reconcile.py — the CLI entry point.

The orchestration success path is covered against a fake calendar client in
`test_calendar_reconcile.py`; this exercises the script's outer contract: a
config failure surfaces as a non-zero exit + safe-shape JSON, never a raw
traceback the agent can't parse.

The gateway / tier cases run the script against a local `http.server` fixture
(via GOOGLE_CALENDAR_API_BASE) rather than a mock: the script is a subprocess,
so its transport is only reachable over a real socket. Bound to 127.0.0.1 on an
ephemeral port — no outbound network, deterministic.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from state import STATE_SCHEMA_VERSION  # noqa: E402

SCRIPT = REPO_ROOT / "skills" / "flight-assist" / "scripts" / "reconcile.py"

# Stripped from the base env before each run so an ambient value never leaks
# into the assertions or points the script at a real backend.
_STRIPPED_VARS = (
    "COMPOSIO_API_KEY",
    "COMPOSIO_USER_ID",
    "COMPOSIO_BASE_URL",
    "GOOGLE_CALENDAR_API_BASE",
)


def _run(env_overrides: dict) -> subprocess.CompletedProcess:
    """Run reconcile.py with the retired/base-url vars stripped, plus overrides."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_VARS}
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def _configured_state(tmp_path: Path) -> Path:
    """A state dir whose config names a flight calendar, so the reconcile
    actually reaches the calendar API instead of short-circuiting."""
    state_dir = tmp_path / "state" / "flight-assist"
    state_dir.mkdir(parents=True)
    (state_dir / "config.json").write_text(
        json.dumps(
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "byair_calendar_name": "Synthetic Flights",
            }
        )
    )
    return state_dir


@pytest.fixture
def calendar_server():
    """A local HTTP server answering every request with a fixed status + body."""
    responses: dict = {"status": 200, "body": b"{}"}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's API
            self.send_response(responses["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(responses["body"])

        def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's signature
            pass  # keep the test output clean

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    # poll_interval bounds how long shutdown() blocks; the 0.5s default
    # would add half a second of teardown to every test using this fixture.
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    responses["base_url"] = f"http://{host}:{port}/calendar/v3"
    try:
        yield responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_gateway_not_injecting_exits_nonzero_with_safe_json(tmp_path, calendar_server):
    """A 401 means the OneCLI gateway did not authenticate the request — a
    config failure the operator must fix, reported as `gateway` rather than as
    a traceback or a silently-empty cycle."""
    calendar_server["status"] = 401
    calendar_server["body"] = b'{"error": "invalid_credentials"}'
    result = _run(
        {
            "GOOGLE_CALENDAR_API_BASE": calendar_server["base_url"],
            "FLIGHT_ASSIST_STATE_DIR": str(_configured_state(tmp_path)),
        }
    )
    assert result.returncode == 1
    assert json.loads(result.stdout.strip()) == {"status": "error", "error": "gateway"}
    # the stderr diagnostic tells the operator where to look
    assert "HTTPS_PROXY" in result.stderr


def test_tier_restricted_exits_nonzero_with_safe_json(tmp_path, calendar_server):
    """The untrusted tier is gated from Google by design (#638) — reported
    distinctly from a broken gateway so it never reads as a fault."""
    calendar_server["status"] = 403
    calendar_server["body"] = b'{"error": "access_restricted"}'
    result = _run(
        {
            "GOOGLE_CALENDAR_API_BASE": calendar_server["base_url"],
            "FLIGHT_ASSIST_STATE_DIR": str(_configured_state(tmp_path)),
        }
    )
    assert result.returncode == 1
    assert json.loads(result.stdout.strip()) == {"status": "error", "error": "tier"}


def test_no_composio_env_is_needed_to_run(tmp_path, calendar_server):
    """#638: the container holds no Google credential. With the retired
    COMPOSIO_* vars absent, the script must still run a clean cycle — the
    missing-credentials exit it used to take is gone."""
    calendar_server["status"] = 200
    calendar_server["body"] = json.dumps({"items": []}).encode()
    result = _run(
        {
            "GOOGLE_CALENDAR_API_BASE": calendar_server["base_url"],
            "FLIGHT_ASSIST_STATE_DIR": str(_configured_state(tmp_path)),
        }
    )
    assert result.returncode == 0
    # no calendar matched the configured name → nothing to reconcile, cleanly
    assert json.loads(result.stdout.strip())["status"] == "no_calendar"


def test_state_failure_is_not_mislabeled(tmp_path):
    """A corrupt-state failure surfaces as `state`, under its real cause.

    The gateway / tier catches are scoped to their own exception types, so a
    StateError raised inside the reconcile run can't be swallowed by one of
    them and mislabeled as a config problem.
    """
    state_dir = tmp_path / "state" / "flight-assist"
    state_dir.mkdir(parents=True)
    # schema_version above the module's current → read_config raises StateError
    # (forward incompatibility), exercised through the reconcile run.
    (state_dir / "config.json").write_text('{"schema_version": 999}')
    result = _run({"FLIGHT_ASSIST_STATE_DIR": str(state_dir)})
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
    # No config.json → run_reconcile resolves no flight calendar → no_calendar,
    # so the cycle completes without reaching the calendar API.
    result = _run(
        {
            "FLIGHT_ASSIST_STATE_DIR": str(state_dir),
            "GOOGLE_MAPS_API_KEY": "synthetic_maps_key",
            "BYAIR_MCP_URL": "https://example.invalid/mcp/synthetic",
        }
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout.strip())  # single-line JSON, not a traceback
    assert payload["status"] == "no_calendar"
    assert payload["airport_drive"] == {"status": "retired", "engine": "drive-engine"}
