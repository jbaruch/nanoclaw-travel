"""Tests for the drive-or-fly verdict store.

Every test points `DRIVE_PLANNER_STATE_DIR` at a tmp_path, so no test sees
another's file and none touches the deployed store. Instants are fixed; the
module never reads a clock, `now` is always passed in.

The behaviours pinned here are the ones a bug in would be invisible: an
operator's answer surviving the next sweep, a question being asked once, and a
corrupt file refusing to read as "no verdicts" (which would silently re-ask
about every trip already settled).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from drive_decision import (  # noqa: E402
    DECIDED_BY_DRIVE_TIME,
    DECIDED_BY_OPERATOR,
    DECISION_SCHEMA_VERSION,
    VERDICT_DRIVE,
    VERDICT_FLY,
    VERDICT_UNKNOWN,
    DriveDecisionError,
    load_verdicts,
    mark_asked,
    prune,
    record_drive_time,
    record_operator_answer,
)

UTC = timezone.utc
NOW = datetime(2020, 8, 7, 12, 0, tzinfo=UTC)
EXPIRES = datetime(2020, 8, 17, 0, 0, tzinfo=UTC)
KEY = "tn-tigers-2020-08"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    return tmp_path


def _store_file(tmp_path: Path) -> Path:
    return tmp_path / "drive-decisions.json"


def _write_raw(tmp_path: Path, payload) -> None:
    _store_file(tmp_path).write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_no_file_means_no_verdicts():
    """A store that was never written is indistinguishable from an empty one."""
    assert load_verdicts(NOW) == {}


def test_record_drive_time_round_trips():
    record_drive_time(KEY, verdict=VERDICT_UNKNOWN, drive_seconds=13200, expires=EXPIRES, now=NOW)
    stored = load_verdicts(NOW)[KEY]
    assert stored.verdict == VERDICT_UNKNOWN
    assert stored.decided_by == DECIDED_BY_DRIVE_TIME
    assert stored.drive_seconds == 13200
    assert stored.asked_at is None
    assert stored.expires == EXPIRES


def test_a_fresh_ambiguous_record_owes_a_question():
    verdict = record_drive_time(
        KEY, verdict=VERDICT_UNKNOWN, drive_seconds=13200, expires=EXPIRES, now=NOW
    )
    assert verdict.needs_question is True


def test_mark_asked_settles_the_question_debt():
    record_drive_time(KEY, verdict=VERDICT_UNKNOWN, drive_seconds=13200, expires=EXPIRES, now=NOW)
    mark_asked(KEY, now=NOW)
    assert load_verdicts(NOW)[KEY].needs_question is False
    assert load_verdicts(NOW)[KEY].asked_at == NOW


def test_mark_asked_is_idempotent():
    """A second sweep must not restamp the ask and reopen the window."""
    record_drive_time(KEY, verdict=VERDICT_UNKNOWN, drive_seconds=13200, expires=EXPIRES, now=NOW)
    mark_asked(KEY, now=NOW)
    mark_asked(KEY, now=NOW + timedelta(hours=1))
    assert load_verdicts(NOW)[KEY].asked_at == NOW


def test_mark_asked_without_a_record_is_a_caller_bug():
    with pytest.raises(DriveDecisionError, match="record_drive_time"):
        mark_asked(KEY, now=NOW)


# ---------------------------------------------------------------------------
# Precedence — the whole reason the store exists
# ---------------------------------------------------------------------------


def test_an_operator_answer_survives_the_next_sweep():
    """The sweep re-derives the band every 30 minutes; if that overwrote the
    answer the engine would re-ask forever."""
    record_drive_time(KEY, verdict=VERDICT_UNKNOWN, drive_seconds=13200, expires=EXPIRES, now=NOW)
    record_operator_answer(KEY, VERDICT_DRIVE, now=NOW)

    later = NOW + timedelta(minutes=30)
    returned = record_drive_time(
        KEY, verdict=VERDICT_UNKNOWN, drive_seconds=13500, expires=EXPIRES, now=later
    )
    assert returned.verdict == VERDICT_DRIVE
    assert returned.decided_by == DECIDED_BY_OPERATOR
    assert load_verdicts(later)[KEY].verdict == VERDICT_DRIVE


def test_an_expired_operator_answer_no_longer_blocks_a_re_derivation():
    """Past its expiry the answer is stale, not authoritative."""
    record_drive_time(KEY, verdict=VERDICT_UNKNOWN, drive_seconds=13200, expires=EXPIRES, now=NOW)
    record_operator_answer(KEY, VERDICT_DRIVE, now=NOW)

    after = EXPIRES + timedelta(days=1)
    returned = record_drive_time(
        KEY,
        verdict=VERDICT_FLY,
        drive_seconds=30000,
        expires=after + timedelta(days=5),
        now=after,
    )
    assert returned.verdict == VERDICT_FLY
    assert returned.decided_by == DECIDED_BY_DRIVE_TIME


def test_the_ask_stamp_survives_a_re_derivation():
    """Otherwise every sweep would clear it and re-ask."""
    record_drive_time(KEY, verdict=VERDICT_UNKNOWN, drive_seconds=13200, expires=EXPIRES, now=NOW)
    mark_asked(KEY, now=NOW)
    record_drive_time(
        KEY,
        verdict=VERDICT_UNKNOWN,
        drive_seconds=13500,
        expires=EXPIRES,
        now=NOW + timedelta(minutes=30),
    )
    assert load_verdicts(NOW)[KEY].asked_at == NOW


def test_an_answer_for_an_unknown_trip_needs_an_expiry():
    with pytest.raises(DriveDecisionError, match="expires"):
        record_operator_answer(KEY, VERDICT_DRIVE, now=NOW)


def test_an_answer_for_an_unknown_trip_records_with_an_expiry():
    record_operator_answer(KEY, VERDICT_FLY, now=NOW, expires=EXPIRES)
    assert load_verdicts(NOW)[KEY].verdict == VERDICT_FLY


@pytest.mark.parametrize("answer", [VERDICT_UNKNOWN, "maybe", "", None])
def test_only_drive_or_fly_are_answerable(answer):
    """`unknown` is a computed state, never something the operator says."""
    with pytest.raises(DriveDecisionError):
        record_operator_answer(KEY, answer, now=NOW, expires=EXPIRES)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_an_expired_verdict_is_not_loaded():
    record_drive_time(KEY, verdict=VERDICT_FLY, drive_seconds=30000, expires=EXPIRES, now=NOW)
    assert load_verdicts(EXPIRES + timedelta(seconds=1)) == {}


def test_prune_drops_expired_records_and_is_idempotent(tmp_path):
    record_drive_time(KEY, verdict=VERDICT_FLY, drive_seconds=30000, expires=EXPIRES, now=NOW)
    after = EXPIRES + timedelta(days=1)
    assert prune(after) == 1
    assert prune(after) == 0
    assert json.loads(_store_file(tmp_path).read_text())["trips"] == {}


def test_load_does_not_rewrite_the_file(tmp_path):
    """A read is a read; only a write call may touch the store."""
    record_drive_time(KEY, verdict=VERDICT_FLY, drive_seconds=30000, expires=EXPIRES, now=NOW)
    before = _store_file(tmp_path).read_text()
    load_verdicts(EXPIRES + timedelta(days=1))
    assert _store_file(tmp_path).read_text() == before


# ---------------------------------------------------------------------------
# Degraded files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps([1, 2, 3]),
        json.dumps({"trips": {}}),
        json.dumps({"schema_version": "1", "trips": {}}),
        json.dumps({"schema_version": DECISION_SCHEMA_VERSION, "trips": []}),
    ],
    ids=["unparseable", "not-an-object", "no-version", "non-int-version", "trips-not-object"],
)
def test_a_corrupt_file_raises_rather_than_reading_as_empty(tmp_path, payload):
    """Reading a corrupt store as "no verdicts" would drop every answer and
    re-ask about every settled trip — the nag the store exists to prevent."""
    _store_file(tmp_path).write_text(payload, encoding="utf-8")
    with pytest.raises(DriveDecisionError):
        load_verdicts(NOW)


def test_a_newer_schema_version_refuses_both_read_and_write(tmp_path):
    _write_raw(tmp_path, {"schema_version": DECISION_SCHEMA_VERSION + 1, "trips": {}})
    with pytest.raises(DriveDecisionError, match="newer than this"):
        load_verdicts(NOW)
    with pytest.raises(DriveDecisionError, match="newer than this"):
        record_drive_time(KEY, verdict=VERDICT_DRIVE, drive_seconds=100, expires=EXPIRES, now=NOW)


def test_an_older_schema_version_refuses_rather_than_guessing(tmp_path):
    _write_raw(tmp_path, {"schema_version": DECISION_SCHEMA_VERSION - 1, "trips": {}})
    with pytest.raises(DriveDecisionError, match="below the current floor"):
        load_verdicts(NOW)


@pytest.mark.parametrize(
    "record",
    [
        {"verdict": "drive", "decided_by": "operator"},
        {"verdict": "sideways", "decided_by": "operator", "expires": EXPIRES.isoformat()},
        {"verdict": "drive", "decided_by": "a-coin-flip", "expires": EXPIRES.isoformat()},
        {"verdict": "drive", "decided_by": "operator", "expires": "whenever"},
    ],
    ids=["no-expiry", "bad-verdict", "bad-decider", "bad-expiry"],
)
def test_one_malformed_record_is_dropped_without_failing_the_others(tmp_path, record):
    """Unlike a corrupt FILE, a single bad record only means that one trip has
    no verdict — the engine re-derives it this sweep."""
    good = {
        "verdict": VERDICT_DRIVE,
        "decided_by": DECIDED_BY_OPERATOR,
        "drive_seconds": 900,
        "asked_at": None,
        "expires": EXPIRES.isoformat(),
    }
    _write_raw(
        tmp_path,
        {"schema_version": DECISION_SCHEMA_VERSION, "trips": {"bad": record, "good": good}},
    )
    assert list(load_verdicts(NOW)) == ["good"]


def test_a_write_leaves_no_temp_file_behind(tmp_path):
    record_drive_time(KEY, verdict=VERDICT_DRIVE, drive_seconds=900, expires=EXPIRES, now=NOW)
    assert [p.name for p in tmp_path.iterdir()] == ["drive-decisions.json"]


@pytest.mark.parametrize("bad_now", [None, "2020-08-07", datetime(2020, 8, 7, 12, 0)])
def test_a_naive_or_missing_now_is_rejected(bad_now):
    """Comparing a naive instant against the store's UTC expiries would be
    wrong, not merely an exception."""
    with pytest.raises(DriveDecisionError):
        load_verdicts(bad_now)
