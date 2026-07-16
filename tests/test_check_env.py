"""Tests for skills/flight-assist/scripts/check-env.py."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent / "skills" / "flight-assist" / "scripts" / "check-env.py"
)

# Every credential the script reports on, plus the retired COMPOSIO_* pair.
# Stripped from the base env before each run so ambient values never leak into
# the assertions — overrides then re-add only what a case sets. The retired
# vars stay in this list so `test_retired_composio_vars_are_not_reported` can
# set them and prove they are ignored.
CREDS = (
    "BYAIR_MCP_URL",
    "GOOGLE_MAPS_API_KEY",
    "COMPOSIO_API_KEY",
    "COMPOSIO_USER_ID",
)

ALL_FLAGS = {
    "byair_url_present",
    "maps_key_present",
}


def _run(env_overrides: dict) -> dict:
    """Run check-env.py with a clean cred env plus the given overrides."""
    env = {k: v for k, v in os.environ.items() if k not in CREDS}
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip())


def test_all_missing_returns_false_flags():
    out = _run({})
    assert out == {flag: False for flag in ALL_FLAGS}


def test_byair_url_present_true_when_set():
    out = _run({"BYAIR_MCP_URL": "https://api.byairapp.com/mcp?api_key=test_synthetic_value"})
    assert out["byair_url_present"] is True
    assert out["maps_key_present"] is False


def test_maps_key_present_true_when_set():
    out = _run({"GOOGLE_MAPS_API_KEY": "AIzaSy_synthetic_test_value"})
    assert out["maps_key_present"] is True
    assert out["byair_url_present"] is False


def test_retired_composio_vars_are_not_reported():
    """#638 moved calendar access to the OneCLI-brokered native Google REST
    API: the container holds no Google credential, so COMPOSIO_API_KEY /
    COMPOSIO_USER_ID mean nothing. A stale value left in the env must not
    resurrect a flag — reporting one would tell the operator a retired variable
    still gates the calendar."""
    out = _run(
        {
            "COMPOSIO_API_KEY": "comp_synthetic_test_value",
            "COMPOSIO_USER_ID": "user_synthetic_test_value",
        }
    )
    assert out == {flag: False for flag in ALL_FLAGS}


def test_all_present_returns_true_flags():
    out = _run(
        {
            "BYAIR_MCP_URL": "https://api.byairapp.com/mcp?api_key=test_synthetic_value",
            "GOOGLE_MAPS_API_KEY": "AIzaSy_synthetic_test_value",
        }
    )
    assert out == {flag: True for flag in ALL_FLAGS}


def test_empty_string_is_treated_as_missing():
    out = _run({"BYAIR_MCP_URL": "", "GOOGLE_MAPS_API_KEY": ""})
    assert out == {flag: False for flag in ALL_FLAGS}


def test_exit_code_always_zero_even_when_missing():
    env = {k: v for k, v in os.environ.items() if k not in CREDS}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_output_is_single_line_json():
    env = {k: v for k, v in os.environ.items() if k not in CREDS}
    out_raw = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # Strip trailing newline from print(); the payload itself must be one line
    payload = out_raw.rstrip("\n")
    assert "\n" not in payload
    parsed = json.loads(payload)
    assert set(parsed.keys()) == ALL_FLAGS
