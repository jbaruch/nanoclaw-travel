import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def build_travel_db(tmp_path, monkeypatch):
    """Load check-travel-bookings/scripts/build-travel-db.py with
    SCHEDULE_PATH + DB_PATH redirected at tmp_path. Returned tuple is
    (module, schedule_path, db_path)."""
    schedule_path = tmp_path / "travel-schedule.json"
    db_path = tmp_path / "travel-db.json"
    module = _load(
        "build_travel_db_under_test",
        "skills/check-travel-bookings/scripts/build-travel-db.py",
    )
    monkeypatch.setattr(module, "SCHEDULE_PATH", str(schedule_path))
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, schedule_path, db_path


@pytest.fixture
def check_travel_bookings(tmp_path, monkeypatch):
    """Load check-travel-bookings/scripts/check-travel-bookings.py
    with `DB_PATH` and `STATE_PATH` redirected at tmp_path. Returned
    tuple is (module, db_path, state_path) — neither file is created
    so callers choose between absent / present."""
    db_path = tmp_path / "travel-db.json"
    state_path = tmp_path / "travel-booking-state.json"
    module = _load(
        "check_travel_bookings_under_test",
        "skills/check-travel-bookings/scripts/check-travel-bookings.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(module, "STATE_PATH", str(state_path))
    return module, db_path, state_path
