"""Baseline tests for skills/check-travel-bookings/scripts/update-travel-booking-state.py.

Locks the script-delegation-driven mutation contract per `coding-policy: script-delegation`:

  - Single-purpose: snooze writes `{schema_version: 1, snooze_until}` for the slug;
    resolve removes the entry. Other actions are rejected at argparse time
  - Validation: --until is required for snooze and must be a valid ISO date
  - Atomic write via same-dir `.tmp` + `os.replace` (no half-written state)
  - stdout is single-line JSON `{action, slug, state}` on success; stderr +
    non-zero exit on validation failure
  - schema_version is stamped on every snooze write per state-schema.md
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "skills/check-travel-bookings/scripts/update-travel-booking-state.py"


@pytest.fixture
def update_script(tmp_path):
    """Load update-travel-booking-state as a module and return
    (module, state_path)."""
    spec = importlib.util.spec_from_file_location("update_travel_booking_state", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state_path = tmp_path / "travel-booking-state.json"
    return module, state_path


def _argv(state_path: Path, *args: str) -> list[str]:
    return ["--state-path", str(state_path), *args]


def test_snooze_writes_entry_with_schema_version(update_script, capsys):
    module, state_path = update_script
    code = module.main(
        _argv(state_path, "--slug", "madrid-2026-06", "--action", "snooze", "--until", "2026-07-01")
    )
    assert code == 0
    state = json.loads(state_path.read_text())
    assert state == {
        "madrid-2026-06": {"schema_version": 1, "snooze_until": "2026-07-01"},
    }
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "action": "snooze",
        "slug": "madrid-2026-06",
        "state": state,
    }


def test_resolve_removes_entry(update_script, capsys):
    module, state_path = update_script
    state_path.write_text(
        json.dumps(
            {
                "madrid-2026-06": {"schema_version": 1, "snooze_until": "2026-07-01"},
                "lisbon-2026-07": {"schema_version": 1, "snooze_until": "2026-08-01"},
            }
        )
    )
    code = module.main(_argv(state_path, "--slug", "madrid-2026-06", "--action", "resolve"))
    assert code == 0
    state = json.loads(state_path.read_text())
    assert "madrid-2026-06" not in state
    assert "lisbon-2026-07" in state  # unaffected


def test_resolve_missing_slug_is_idempotent(update_script):
    module, state_path = update_script
    code = module.main(_argv(state_path, "--slug", "nonexistent-2026-01", "--action", "resolve"))
    assert code == 0
    assert json.loads(state_path.read_text()) == {}


def test_snooze_without_until_fails(update_script, capsys):
    module, state_path = update_script
    code = module.main(_argv(state_path, "--slug", "madrid-2026-06", "--action", "snooze"))
    assert code == 1
    err = capsys.readouterr().err
    assert "--until is required" in err
    assert not state_path.exists()


def test_snooze_with_invalid_iso_until_fails(update_script, capsys):
    module, state_path = update_script
    code = module.main(
        _argv(state_path, "--slug", "madrid-2026-06", "--action", "snooze", "--until", "tomorrow")
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "is not a valid" in err
    assert not state_path.exists()


def test_snooze_preserves_other_entries(update_script):
    module, state_path = update_script
    state_path.write_text(
        json.dumps({"lisbon-2026-07": {"schema_version": 1, "snooze_until": "2026-08-01"}})
    )
    code = module.main(
        _argv(state_path, "--slug", "madrid-2026-06", "--action", "snooze", "--until", "2026-07-01")
    )
    assert code == 0
    state = json.loads(state_path.read_text())
    assert set(state.keys()) == {"lisbon-2026-07", "madrid-2026-06"}


def test_missing_state_file_treated_as_empty(update_script):
    module, state_path = update_script
    assert not state_path.exists()
    code = module.main(
        _argv(state_path, "--slug", "madrid-2026-06", "--action", "snooze", "--until", "2026-07-01")
    )
    assert code == 0
    state = json.loads(state_path.read_text())
    assert "madrid-2026-06" in state


def test_corrupt_state_file_treated_as_empty(update_script):
    """A pre-existing corrupt state JSON resets to {} on write, since
    snooze data is purely advisory — operator can re-snooze if needed,
    and an unreadable file can't block resolve operations."""
    module, state_path = update_script
    state_path.write_text("{not valid json")
    code = module.main(
        _argv(state_path, "--slug", "madrid-2026-06", "--action", "snooze", "--until", "2026-07-01")
    )
    assert code == 0
    state = json.loads(state_path.read_text())
    assert state == {"madrid-2026-06": {"schema_version": 1, "snooze_until": "2026-07-01"}}


def test_invalid_action_rejected_at_argparse(update_script, capsys, monkeypatch):
    """argparse `choices=` rejects unknown --action values before
    main() runs its body."""
    module, state_path = update_script
    with pytest.raises(SystemExit):
        module.main(_argv(state_path, "--slug", "x-2026-01", "--action", "explode"))


def test_atomic_write_oserror_surfaces_as_diagnostic(update_script, capsys, monkeypatch):
    """_atomic_write() failure (PermissionError, ENOSPC, cross-device
    EXDEV, etc.) surfaces as a clean stderr diagnostic + exit 1
    instead of an uncaught traceback. os.replace is atomic so partial
    state is impossible."""
    module, state_path = update_script

    def _raises(*_a, **_kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(module, "_atomic_write", _raises)
    code = module.main(
        _argv(state_path, "--slug", "madrid-2026-06", "--action", "snooze", "--until", "2026-07-01")
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "update-travel-booking-state:" in err
    assert "PermissionError" in err
