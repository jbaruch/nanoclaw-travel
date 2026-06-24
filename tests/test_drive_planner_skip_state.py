"""Tests for the drive-planner skip store (`skip_state.py`).

Exercises the documented contract (per `coding-policy: stateful-artifacts`
and `skills/drive-planner/state-schema.md`): add / load / clear / prune,
auto-expiry, schema validation, corrupt-file refusal, missing-file
tolerance, atomic writes, and input guards. The state directory is
redirected at a tmp_path via `DRIVE_PLANNER_STATE_DIR`. A final integration
check confirms the loaded mapping is exactly what `scan()` consumes.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-planner"))

from scan import scan  # noqa: E402
from skip_state import (  # noqa: E402
    SKIP_SCHEMA_VERSION,
    SkipStateError,
    add_skip,
    clear_skip,
    load_active_skips,
    prune,
    state_dir,
)

CT = timezone(timedelta(hours=-5))
NOW = datetime(2026, 7, 1, 8, 0, tzinfo=CT)
LATER = NOW + timedelta(hours=4)
EARLIER = NOW - timedelta(hours=1)


@pytest.fixture
def skip_env(tmp_path, monkeypatch):
    """Point DRIVE_PLANNER_STATE_DIR at a tmp dir; yield the skip file path."""
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    return tmp_path / "skip-state.json"


# --- add / load round trip ------------------------------------------------


def test_add_then_load_returns_active_skip(skip_env):
    add_skip("evt_1", expires=LATER, now=NOW)
    assert load_active_skips(NOW) == {"evt_1": LATER.isoformat()}


def test_file_carries_schema_version_and_skips(skip_env):
    add_skip("evt_1", expires=LATER, now=NOW)
    payload = json.loads(skip_env.read_text())
    assert payload["schema_version"] == SKIP_SCHEMA_VERSION
    assert payload["skips"] == {"evt_1": LATER.isoformat()}


def test_load_missing_file_is_empty(skip_env):
    assert not skip_env.exists()
    assert load_active_skips(NOW) == {}


def test_expired_skip_is_filtered_from_load(skip_env):
    add_skip("stale", expires=EARLIER, now=EARLIER - timedelta(hours=1))
    # EARLIER is before NOW → expired at NOW
    assert load_active_skips(NOW) == {}


def test_state_dir_follows_env(skip_env, tmp_path):
    assert state_dir() == tmp_path


# --- idempotency, pruning -------------------------------------------------


def test_reskip_updates_expiry(skip_env):
    add_skip("evt_1", expires=LATER, now=NOW)
    newer = LATER + timedelta(days=1)
    add_skip("evt_1", expires=newer, now=NOW)
    assert load_active_skips(NOW) == {"evt_1": newer.isoformat()}


def test_add_prunes_expired_entries(skip_env):
    add_skip("stale", expires=NOW + timedelta(minutes=1), now=NOW)
    # advance: add a fresh skip at a time the first one has expired
    future = NOW + timedelta(hours=1)
    add_skip("fresh", expires=future + timedelta(hours=2), now=future)
    payload = json.loads(skip_env.read_text())
    assert "stale" not in payload["skips"]
    assert "fresh" in payload["skips"]


def test_prune_removes_expired_and_counts(skip_env):
    add_skip("a", expires=NOW + timedelta(minutes=1), now=NOW)
    add_skip("b", expires=LATER, now=NOW)
    removed = prune(NOW + timedelta(minutes=2))  # "a" expired, "b" still live
    assert removed == 1
    assert load_active_skips(NOW + timedelta(minutes=2)) == {"b": LATER.isoformat()}


def test_prune_noop_returns_zero(skip_env):
    add_skip("b", expires=LATER, now=NOW)
    assert prune(NOW) == 0


# --- clear ----------------------------------------------------------------


def test_clear_removes_present_skip(skip_env):
    add_skip("evt_1", expires=LATER, now=NOW)
    assert clear_skip("evt_1", now=NOW) is True
    assert load_active_skips(NOW) == {}


def test_clear_absent_skip_returns_false(skip_env):
    add_skip("evt_1", expires=LATER, now=NOW)
    assert clear_skip("other", now=NOW) is False
    assert load_active_skips(NOW) == {"evt_1": LATER.isoformat()}


# --- schema / corruption refusal ------------------------------------------


def test_unparseable_file_raises(skip_env):
    skip_env.parent.mkdir(parents=True, exist_ok=True)
    skip_env.write_text("{not json")
    with pytest.raises(SkipStateError, match="not valid JSON"):
        load_active_skips(NOW)


def test_non_object_root_raises(skip_env):
    skip_env.parent.mkdir(parents=True, exist_ok=True)
    skip_env.write_text("[1, 2, 3]")
    with pytest.raises(SkipStateError, match="JSON object"):
        load_active_skips(NOW)


def test_missing_schema_version_raises(skip_env):
    skip_env.parent.mkdir(parents=True, exist_ok=True)
    skip_env.write_text(json.dumps({"skips": {}}))
    with pytest.raises(SkipStateError, match="schema_version"):
        load_active_skips(NOW)


def test_newer_schema_version_raises(skip_env):
    skip_env.parent.mkdir(parents=True, exist_ok=True)
    skip_env.write_text(json.dumps({"schema_version": SKIP_SCHEMA_VERSION + 1, "skips": {}}))
    with pytest.raises(SkipStateError, match="newer"):
        load_active_skips(NOW)


def test_older_schema_version_is_refused_not_passed_through(skip_env):
    # A below-floor version must be detected explicitly, not silently treated
    # as current (per stateful-artifacts owner-migration handling).
    skip_env.parent.mkdir(parents=True, exist_ok=True)
    skip_env.write_text(json.dumps({"schema_version": SKIP_SCHEMA_VERSION - 1, "skips": {}}))
    with pytest.raises(SkipStateError, match="below the current"):
        load_active_skips(NOW)


def test_malformed_entries_are_dropped(skip_env):
    skip_env.parent.mkdir(parents=True, exist_ok=True)
    skip_env.write_text(
        json.dumps(
            {
                "schema_version": SKIP_SCHEMA_VERSION,
                "skips": {
                    "good": LATER.isoformat(),
                    "naive_expiry": "2026-07-01T17:00:00",  # naive → inactive
                    "bad_expiry": "not-a-date",
                },
            }
        )
    )
    assert load_active_skips(NOW) == {"good": LATER.isoformat()}


def test_z_suffix_expiry_is_honored(skip_env):
    skip_env.parent.mkdir(parents=True, exist_ok=True)
    skip_env.write_text(
        json.dumps(
            {"schema_version": SKIP_SCHEMA_VERSION, "skips": {"evt_1": "2026-07-01T20:00:00Z"}}
        )
    )
    assert load_active_skips(NOW) == {"evt_1": "2026-07-01T20:00:00Z"}


# --- input guards ---------------------------------------------------------


def test_naive_expires_raises(skip_env):
    with pytest.raises(SkipStateError, match="expires"):
        add_skip("evt_1", expires=datetime(2026, 7, 1, 12, 0), now=NOW)


@pytest.mark.parametrize(
    "call",
    [
        lambda: add_skip("evt_1", expires=LATER, now=datetime(2026, 7, 1, 8, 0)),
        lambda: load_active_skips(datetime(2026, 7, 1, 8, 0)),
        lambda: clear_skip("evt_1", now=datetime(2026, 7, 1, 8, 0)),
        lambda: prune(datetime(2026, 7, 1, 8, 0)),
    ],
)
def test_naive_now_raises(skip_env, call):
    with pytest.raises(SkipStateError, match="now"):
        call()


@pytest.mark.parametrize("bad", ["", 123, None])
def test_empty_meeting_id_raises(skip_env, bad):
    with pytest.raises(SkipStateError, match="meeting_id"):
        add_skip(bad, expires=LATER, now=NOW)  # type: ignore[arg-type]


# --- atomic write hygiene -------------------------------------------------


def test_no_temp_file_left_behind(skip_env, tmp_path):
    add_skip("evt_1", expires=LATER, now=NOW)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# --- integration: the loaded mapping is scan-compatible -------------------


def test_loaded_skips_drive_scan_to_skipped(skip_env):
    start = NOW + timedelta(hours=3)
    meeting = {
        "id": "evt_1",
        "summary": "Customer sync",
        "location": "100 Broadway, Nashville, TN",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }
    add_skip("evt_1", expires=LATER, now=NOW)
    active = load_active_skips(NOW)
    [result] = scan([meeting], now=NOW, home_address="Home", skip_state=active)
    assert result.bucket == "skipped"
