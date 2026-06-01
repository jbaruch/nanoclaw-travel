"""Baseline tests for nightly-travel-sync/scripts/check-travel-freshness.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - "missing"  — schedule file does not exist; output JSON has only
                 `status` and `path`
  - "fresh"    — file age < 7 days; payload includes `mtime` + `age_days`
  - "stale"    — file age >= 7 days; payload includes `mtime`,
                 `age_days`, plus the Gmail query (`from:tripit.com
                 after:<mtime-1d>`) and `subject_prefix` for re-sync
  - exit code is always 0 (the agent decides what to do based on JSON)

Tests freeze `module.datetime` to a fixed-now subclass so age math is
deterministic, and use `os.utime` to set the schedule file's mtime
explicitly per case.
"""

import json
import os
from datetime import datetime, timezone

_FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _make_frozen_datetime(real_datetime):
    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return _FROZEN_NOW.replace(tzinfo=None)
            return _FROZEN_NOW.astimezone(tz)

        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            return real_datetime.fromtimestamp(ts, tz)

    return FrozenDateTime


def _run(module, monkeypatch, capsys, freeze=True):
    monkeypatch.setattr("sys.argv", ["check-travel-freshness.py"])
    if freeze:
        monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    code = 0
    try:
        result = module.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _set_mtime(path, frozen_now, days_ago):
    """Set file mtime to (frozen_now - days_ago days)."""
    target = frozen_now.timestamp() - days_ago * 86400
    os.utime(str(path), (target, target))


def test_missing_file_emits_missing_status(check_travel_freshness, monkeypatch, capsys):
    module, schedule_path = check_travel_freshness
    assert not schedule_path.exists()
    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert err == ""
    payload = json.loads(out)
    assert payload == {"status": "missing", "path": str(schedule_path)}


def test_fresh_file_emits_fresh_status(check_travel_freshness, monkeypatch, capsys):
    """Day-old file is well within the 7-day fresh window."""
    module, schedule_path = check_travel_freshness
    schedule_path.write_text("[]")
    _set_mtime(schedule_path, _FROZEN_NOW, days_ago=1)
    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["status"] == "fresh"
    assert "mtime" in payload
    assert payload["age_days"] == 1.0
    assert "gmail_query" not in payload


def test_six_day_old_file_still_fresh(check_travel_freshness, monkeypatch, capsys):
    """Boundary: < 7 days = fresh."""
    module, schedule_path = check_travel_freshness
    schedule_path.write_text("[]")
    _set_mtime(schedule_path, _FROZEN_NOW, days_ago=6)
    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["status"] == "fresh"


def test_seven_day_old_file_is_stale(check_travel_freshness, monkeypatch, capsys):
    """Boundary: >= 7 days = stale (script uses `age >= timedelta(days=7)`)."""
    module, schedule_path = check_travel_freshness
    schedule_path.write_text("[]")
    _set_mtime(schedule_path, _FROZEN_NOW, days_ago=7)
    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["status"] == "stale"
    assert payload["age_days"] == 7.0
    assert payload["gmail_query"].startswith("from:tripit.com after:")
    assert payload["subject_prefix"] == module.SUBJECT_PREFIX


def test_stale_gmail_query_uses_one_day_prebuffer(check_travel_freshness, monkeypatch, capsys):
    """`gmail_query` uses (mtime - 1 day) as the `after:` cutoff to absorb
    Gmail's date-only inclusive boundary semantics."""
    module, schedule_path = check_travel_freshness
    schedule_path.write_text("[]")
    _set_mtime(schedule_path, _FROZEN_NOW, days_ago=10)
    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    # 10 days before frozen now (2026-04-30) is 2026-04-20; -1d buffer = 2026-04-19.
    assert "after:2026/04/19" in payload["gmail_query"]


def test_far_stale_file_emits_stale(check_travel_freshness, monkeypatch, capsys):
    """30 days old — still stale (no upper bound)."""
    module, schedule_path = check_travel_freshness
    schedule_path.write_text("[]")
    _set_mtime(schedule_path, _FROZEN_NOW, days_ago=30)
    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["status"] == "stale"
