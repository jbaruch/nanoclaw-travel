"""Tests for skills/flight-assist/scripts/set-home-base.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "skills" / "flight-assist" / "scripts" / "set-home-base.py"


def _run(args: list[str], *, state_dir: Path) -> tuple[int, str, str]:
    """Run set-home-base.py with FLIGHT_ASSIST_STATE_DIR redirected to state_dir."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env={"FLIGHT_ASSIST_STATE_DIR": str(state_dir), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    return (result.returncode, result.stdout, result.stderr)


def test_writes_home_address(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    code, stdout, _ = _run(["1 Fixture Loop, Cupertino, CA 95014"], state_dir=state)
    assert code == 0
    payload = json.loads(stdout.strip())
    assert payload["home_address"] == "1 Fixture Loop, Cupertino, CA 95014"
    # Verify on-disk write
    config = json.loads((state / "config.json").read_text())
    assert config["home_address"] == "1 Fixture Loop, Cupertino, CA 95014"


def test_overwrites_existing_home_address(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    # Seed a valid existing config — write_config rejects unknown keys, so the
    # seed must match the documented config schema (home_address only).
    (state / "config.json").write_text(
        json.dumps({"schema_version": 1, "home_address": "old address"})
    )
    code, _, _ = _run(["new address"], state_dir=state)
    assert code == 0
    config = json.loads((state / "config.json").read_text())
    assert config["home_address"] == "new address"


def test_missing_argument_exits_2(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    code, _, stderr = _run([], state_dir=state)
    assert code == 2
    assert "usage" in stderr


def test_empty_address_exits_2(tmp_path: Path):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    code, _, stderr = _run(["   "], state_dir=state)
    assert code == 2
    assert "empty" in stderr


@pytest.mark.parametrize(
    "address",
    [
        "1 Infinite Loop, Cupertino, CA",
        "10 Downing Street, London, UK",
        "São Paulo, Brazil",  # Unicode
        "1234 Apt #5B, Brooklyn, NY 11201",  # Special chars
    ],
)
def test_accepts_varied_address_formats(tmp_path: Path, address: str):
    state = tmp_path / "state" / "flight-assist"
    state.mkdir(parents=True)
    code, stdout, _ = _run([address], state_dir=state)
    assert code == 0
    payload = json.loads(stdout.strip())
    assert payload["home_address"] == address
