"""Tests for skills/flight-assist/scripts/check-env.py."""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent / "skills" / "flight-assist" / "scripts" / "check-env.py"
)


def _run(env_overrides: dict) -> dict:
    """Run check-env.py with the given env overrides, return parsed JSON output."""
    env = {k: v for k, v in os.environ.items() if k not in env_overrides}
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip())


def test_both_missing_returns_false_flags():
    out = _run({"BYAIR_MCP_URL": None, "GOOGLE_MAPS_API_KEY": None})
    assert out == {"byair_url_present": False, "maps_key_present": False}


def test_byair_url_present_true_when_set():
    out = _run(
        {
            "BYAIR_MCP_URL": "https://api.byairapp.com/mcp?api_key=test_synthetic_value",
            "GOOGLE_MAPS_API_KEY": None,
        }
    )
    assert out["byair_url_present"] is True
    assert out["maps_key_present"] is False


def test_maps_key_present_true_when_set():
    out = _run(
        {
            "BYAIR_MCP_URL": None,
            "GOOGLE_MAPS_API_KEY": "AIzaSy_synthetic_test_value",
        }
    )
    assert out["byair_url_present"] is False
    assert out["maps_key_present"] is True


def test_both_present_returns_true_flags():
    out = _run(
        {
            "BYAIR_MCP_URL": "https://api.byairapp.com/mcp?api_key=test_synthetic_value",
            "GOOGLE_MAPS_API_KEY": "AIzaSy_synthetic_test_value",
        }
    )
    assert out == {"byair_url_present": True, "maps_key_present": True}


def test_empty_string_is_treated_as_missing():
    out = _run({"BYAIR_MCP_URL": "", "GOOGLE_MAPS_API_KEY": ""})
    assert out == {"byair_url_present": False, "maps_key_present": False}


def test_exit_code_always_zero_even_when_missing():
    creds = ("BYAIR_MCP_URL", "GOOGLE_MAPS_API_KEY")
    env = {k: v for k, v in os.environ.items() if k not in creds}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_output_is_single_line_json():
    creds = ("BYAIR_MCP_URL", "GOOGLE_MAPS_API_KEY")
    env = {k: v for k, v in os.environ.items() if k not in creds}
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
    assert set(parsed.keys()) == {"byair_url_present", "maps_key_present"}
