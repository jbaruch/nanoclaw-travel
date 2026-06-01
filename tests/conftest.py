import importlib.util
import sqlite3 as _sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    assert (
        spec is not None and spec.loader is not None
    ), f"failed to locate {relpath} for fixture {name}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_tz_state_db(db_path: str) -> None:
    """Apply the host `tz_state` singleton DDL to a fresh SQLite file,
    mirroring the orchestrator's state-010/012 migration shape so the
    reader test stays tied to the real schema. The singleton row is NOT
    inserted — callers INSERT per scenario (or leave it empty to exercise
    the no-row branch)."""
    conn = _sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE tz_state (
              id             INTEGER PRIMARY KEY CHECK(id = 1),
              current_tz     TEXT NOT NULL,
              home_tz        TEXT NOT NULL,
              scheduler_tz   TEXT,
              schema_version INTEGER NOT NULL DEFAULT 1,
              segments       TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def read_current_tz(tmp_path, monkeypatch):
    """Load flight-assist/scripts/read-current-tz.py with DB_PATH
    redirected at a tmp_path-rooted SQLite file seeded with the
    `tz_state` schema. Returned tuple is (module, db_path) — the
    singleton row is NOT inserted so callers choose row-present vs
    no-row. SUPPORTED_TZ_STATE_SCHEMA_VERSION on the loaded module is
    the version a present row must carry to be honoured."""
    db_path = tmp_path / "messages.db"
    _seed_tz_state_db(str(db_path))
    module = _load(
        "read_current_tz_under_test",
        "skills/flight-assist/scripts/read-current-tz.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, db_path


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


# --- nightly-travel-sync fixtures (jbaruch/nanoclaw-admin#318) -----------
#
# The travel-source scripts (refresh-travel-schedule, filter-tripit-
# bookings, check-travel-freshness) + their tests moved here from
# `nanoclaw-admin/skills/nightly-external-sync` to finish the #299
# reader-without-writer split: flight-assist now owns the writers of
# the travel-schedule.json / travel-db.json data it consumes.


@pytest.fixture
def refresh_travel_schedule(tmp_path, monkeypatch):
    """Load nightly-travel-sync/scripts/refresh-travel-schedule.py with
    URL_PATH and OUTPUT_PATH redirected at tmp_path. Returned tuple is
    (module, url_path, output_path) — neither file is created so
    callers can choose between 'present' (write content) and 'absent'.

    The script fetches an ICS feed via `urllib.request.urlopen`; tests
    patch the global `urllib.request.urlopen` to avoid network I/O.
    """
    url_path = tmp_path / "tripit-url.txt"
    output_path = tmp_path / "travel-schedule.json"
    module = _load(
        "refresh_travel_schedule_under_test",
        "skills/nightly-travel-sync/scripts/refresh-travel-schedule.py",
    )
    monkeypatch.setattr(module, "URL_PATH", str(url_path))
    monkeypatch.setattr(module, "OUTPUT_PATH", str(output_path))
    return module, url_path, output_path


@pytest.fixture
def filter_tripit_bookings():
    """Load nightly-travel-sync/scripts/filter-tripit-bookings.py.

    Reads JSON from stdin and writes JSON to stdout — no module-level
    state. Tests intercept stdin via `monkeypatch.setattr('sys.stdin',
    _FakeStdin(...))`.
    """
    return _load(
        "filter_tripit_bookings_under_test",
        "skills/nightly-travel-sync/scripts/filter-tripit-bookings.py",
    )


@pytest.fixture
def check_travel_freshness(tmp_path, monkeypatch):
    """Load nightly-travel-sync/scripts/check-travel-freshness.py with
    SCHEDULE_PATH redirected at a tmp_path-rooted file. Returned tuple
    is (module, schedule_path) — the file is NOT created so callers
    exercise both 'missing' and 'present-with-mtime' branches."""
    import pathlib

    schedule_path = tmp_path / "travel-schedule.json"
    module = _load(
        "check_travel_freshness_under_test",
        "skills/nightly-travel-sync/scripts/check-travel-freshness.py",
    )
    monkeypatch.setattr(module, "SCHEDULE_PATH", pathlib.Path(schedule_path))
    return module, schedule_path


@pytest.fixture
def nightly_travel_sync_precheck(tmp_path):
    """Load nightly-travel-sync/precheck.py. Returned tuple is
    (module, db_path) — `db_path` is a tmp_path-rooted Path that is NOT
    created so callers exercise the missing / fresh / stale branches by
    creating it and setting its mtime, then calling
    `module.decide(now, db_path)`."""
    db_path = tmp_path / "travel-db.json"
    module = _load(
        "nightly_travel_sync_precheck_under_test",
        "skills/nightly-travel-sync/precheck.py",
    )
    return module, db_path
