"""Tests for the drive-planner home-address reader (`home_address.py`).

Builds the canonical `## Addresses` block programmatically in a tmp file (no
fixtures checked in, per `coding-policy: testing-standards`) and exercises the
parse plus the actionable-error paths. drive-planner refuses to guess an
origin, so a missing block must raise — these tests pin that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-planner"))

from home_address import HomeAddressError, read_current_home  # noqa: E402

CANONICAL_BLOCK = """\
# Owner Profile

Some prose about the operator.

## Addresses
<!-- canonical, machine-read by travel tile -->
- current_home: 1040 Pine Creek Dr, Arrington, TN 37014
- home_airport: BNA
- new_home_wip: 1835 Burke Hollow Rd, Nolensville, TN 37135
"""


def _write_profile(tmp_path: Path, text: str) -> Path:
    profile = tmp_path / "user_profile.md"
    profile.write_text(text, encoding="utf-8")
    return profile


def test_reads_current_home(tmp_path):
    profile = _write_profile(tmp_path, CANONICAL_BLOCK)
    assert read_current_home(path=profile) == "1040 Pine Creek Dr, Arrington, TN 37014"


def test_does_not_read_new_home_wip(tmp_path):
    # new_home_wip must never be picked up automatically — origin switches
    # are an explicit later change, not whichever address parses first.
    profile = _write_profile(tmp_path, CANONICAL_BLOCK)
    assert "Nolensville" not in read_current_home(path=profile)


def test_tolerates_whitespace_variants(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n-   current_home :   123 Main St, Town  \n")
    assert read_current_home(path=profile) == "123 Main St, Town"


def test_env_override(tmp_path, monkeypatch):
    profile = _write_profile(tmp_path, CANONICAL_BLOCK)
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    assert read_current_home() == "1040 Pine Creek Dr, Arrington, TN 37014"


def test_missing_file_raises_actionable(tmp_path):
    with pytest.raises(HomeAddressError, match="owner profile not found"):
        read_current_home(path=tmp_path / "nope.md")


def test_missing_current_home_entry_raises(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n- home_airport: BNA\n")
    with pytest.raises(HomeAddressError, match="no `current_home:` entry"):
        read_current_home(path=profile)


def test_empty_current_home_value_raises(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n- current_home:   \n")
    with pytest.raises(HomeAddressError, match="no `current_home:` entry"):
        read_current_home(path=profile)
