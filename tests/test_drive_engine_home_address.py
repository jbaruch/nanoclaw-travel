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
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from home_address import HomeAddressError, read_current_home  # noqa: E402

CANONICAL_BLOCK = """\
# Owner Profile

Some prose about the operator.

## Addresses
<!-- canonical, machine-read by travel tile -->
- current_home: 12 Example St, Sampleton, TN 37000
- home_airport: BNA
- new_home_wip: 99 Placeholder Rd, Testburg, TN 37100
"""


def _write_profile(tmp_path: Path, text: str) -> Path:
    profile = tmp_path / "user_profile.md"
    profile.write_text(text, encoding="utf-8")
    return profile


def test_reads_current_home(tmp_path):
    profile = _write_profile(tmp_path, CANONICAL_BLOCK)
    assert read_current_home(path=profile) == "12 Example St, Sampleton, TN 37000"


def test_does_not_read_new_home_wip(tmp_path):
    # new_home_wip must never be picked up automatically — origin switches
    # are an explicit later change, not whichever address parses first.
    profile = _write_profile(tmp_path, CANONICAL_BLOCK)
    assert "Testburg" not in read_current_home(path=profile)


def test_tolerates_whitespace_variants(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n-   current_home :   123 Main St, Town  \n")
    assert read_current_home(path=profile) == "123 Main St, Town"


def test_env_override(tmp_path, monkeypatch):
    profile = _write_profile(tmp_path, CANONICAL_BLOCK)
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    assert read_current_home() == "12 Example St, Sampleton, TN 37000"


def test_missing_file_raises_actionable(tmp_path):
    with pytest.raises(HomeAddressError, match="owner profile not found"):
        read_current_home(path=tmp_path / "nope.md")


def test_missing_current_home_entry_raises(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n- home_airport: BNA\n")
    with pytest.raises(HomeAddressError, match="no `current_home:` entry"):
        read_current_home(path=profile)


def test_missing_addresses_block_raises(tmp_path):
    profile = _write_profile(tmp_path, "# Owner Profile\n\nSome prose, no Addresses block.\n")
    with pytest.raises(HomeAddressError, match="no `## Addresses` block"):
        read_current_home(path=profile)


def test_current_home_outside_addresses_block_is_ignored(tmp_path):
    # A `current_home:` in prose or a later section must NOT set the origin —
    # only the value inside the canonical `## Addresses` block counts.
    text = (
        "# Owner Profile\n\n"
        "- current_home: 999 Stale Prose Rd, Oldtown\n\n"
        "## Addresses\n"
        "- current_home: 12 Example St, Sampleton, TN 37000\n\n"
        "## Notes\n"
        "- current_home: 1 Decoy Ln, Faketown\n"
    )
    profile = _write_profile(tmp_path, text)
    assert read_current_home(path=profile) == "12 Example St, Sampleton, TN 37000"


def test_addresses_block_without_current_home_raises_even_if_prose_has_it(tmp_path):
    text = "- current_home: 999 Stale Rd\n\n## Addresses\n- home_airport: BNA\n"
    profile = _write_profile(tmp_path, text)
    with pytest.raises(HomeAddressError, match="no `current_home:` entry"):
        read_current_home(path=profile)


def test_empty_current_home_value_raises(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n- current_home:   \n")
    with pytest.raises(HomeAddressError, match="no `current_home:` entry"):
        read_current_home(path=profile)
