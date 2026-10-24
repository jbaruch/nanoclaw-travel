"""Net reporting must not turn provider rewrites into new travel."""

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "skills/nightly-travel-sync/scripts/summarize-sync.py"
)
spec = importlib.util.spec_from_file_location("summarize_sync", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TODAY = date(2024, 10, 23)
SEGMENT = {
    "timezone": "Europe/Paris",
    "from": "2024-06-08",
    "to": "2024-06-10",
    "from_dt": "2024-06-08T00:00:00Z",
    "to_dt": "2024-06-10T00:00:00Z",
    "label": "Paris",
}


def successful_payload():
    return {
        "mode": "sync",
        "errors": [],
        "noChanges": True,
        "homeTimezone": "America/Chicago",
        "timezoneChanges": [],
        "segments": [dict(SEGMENT)],
        "conflicts": [],
        "ooo": None,
    }


def change(action="create", zone="Europe/Paris", start="2024-10-24", end="2024-10-26"):
    return {"action": action, "timezone": zone, "from": start, "to": end}


def test_rewriting_the_same_rows_has_no_report():
    payload = {
        "noChanges": False,
        "timezoneChanges": [change("delete"), change()],
        "segments": [{"trip": "An unrelated existing itinerary"}],
        "ooo": {"created": 0, "deleted": 0},
    }
    result = module.summarize(payload, TODAY)
    assert result["timezoneDiff"] == {"added": [], "removed": [], "changed": []}
    assert result["reportLines"] == []
    assert result["segments"] == payload["segments"]
    assert payload.get("reportLines") is None


def test_end_date_today_is_not_a_new_block_and_named_ooo_stays_separate():
    result = module.summarize(
        {
            "timezoneChanges": [
                change("delete"),
                change(),
                change(start="2024-10-22", end="2024-10-23"),
            ],
            "ooo": {"created": 1, "createdNames": ["Future conference"]},
        },
        TODAY,
    )
    assert result["timezoneDiff"]["added"] == []
    assert result["reportLines"] == ["OOO: создан блок для поездки «Future conference»."]


def test_added_removed_and_changed_rows_are_deterministic():
    result = module.summarize(
        {
            "timezoneChanges": [
                change("delete"),
                change(zone="Europe/Berlin"),
                change(start="2024-10-27", end="2024-10-29"),
                change("delete", start="2024-10-30", end="2024-11-01"),
            ]
        },
        TODAY,
    )
    diff = result["timezoneDiff"]
    assert diff["changed"] == [
        {
            "from": "2024-10-24",
            "to": "2024-10-26",
            "oldTimezone": "Europe/Paris",
            "newTimezone": "Europe/Berlin",
        }
    ]
    assert len(diff["added"]) == len(diff["removed"]) == 1
    assert len(result["reportLines"]) == 3


def test_duplicate_rows_are_netted_as_a_multiset():
    result = module.summarize({"timezoneChanges": [change("delete"), change(), change()]}, TODAY)
    assert len(result["timezoneDiff"]["added"]) == 1


def test_ambiguous_pairing_retains_explicit_additions_and_removals():
    result = module.summarize(
        {
            "timezoneChanges": [
                change("delete"),
                change(zone="Europe/Berlin"),
                change(zone="Europe/London"),
            ]
        },
        TODAY,
    )
    assert result["timezoneDiff"]["changed"] == []
    assert len(result["timezoneDiff"]["added"]) == 2
    assert len(result["timezoneDiff"]["removed"]) == 1


def test_ooo_lines_name_each_actual_created_and_deleted_trip():
    result = module.summarize(
        {
            "ooo": {
                "created": 2,
                "deleted": 1,
                "prioritySet": 2,
                "createdNames": ["Conference A", "Trip\nB"],
                "deletedNames": ["Old C"],
            }
        },
        TODAY,
    )
    assert result["reportLines"] == [
        "OOO: создан блок для поездки «Conference A».",
        "OOO: создан блок для поездки «Trip B».",
        "OOO: удалён блок для поездки «Old C».",
        "OOO: приоритет P2 установлен для блоков: 2.",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"timezoneChanges": {}},
        {"timezoneChanges": [change("unknown")]},
        {"timezoneChanges": [change(start="invalid")]},
        {"timezoneChanges": [change(end="2024-10-24")]},
        {"ooo": {"created": 1}},
        {"ooo": {"created": 0, "createdNames": ["Unbound"]}},
        {"ooo": {"created": True, "createdNames": ["Wrong count"]}},
        {"ooo": {"prioritySet": -1}},
    ],
)
def test_incomplete_or_ambiguous_provider_output_is_not_reported(payload):
    with pytest.raises((ValueError, TypeError)):
        module.summarize(payload, TODAY)


def test_cli_preserves_errors_and_reports_one_json_object():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"errors": ["Provider failed"]}),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["errors"] == ["Provider failed"]
    assert json.loads(result.stdout)["reportLines"] == []


def test_cli_rejects_invalid_json_without_a_partial_report():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input="not JSON", text=True, capture_output=True
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Inspect the sync JSON" in result.stderr


@pytest.mark.parametrize("payload", [{}, {"errors": []}, {"timezoneChanges": []}])
def test_cli_rejects_missing_success_fields_instead_of_reporting_no_change(payload):
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(payload), text=True, capture_output=True
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Inspect the sync JSON" in result.stderr


@pytest.mark.parametrize(
    "key,value", [("noChanges", 1), ("segments", {}), ("conflicts", None), ("homeTimezone", False)]
)
def test_cli_rejects_invalid_success_field_types(key, value):
    payload = {
        "errors": [],
        "mode": "sync",
        "noChanges": True,
        "homeTimezone": "Europe/Paris",
        "segments": [],
        "conflicts": [],
        "ooo": None,
        "timezoneChanges": [],
    }
    payload[key] = value
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(payload), text=True, capture_output=True
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "invalid field types" in result.stderr


@pytest.mark.parametrize(
    "key,value",
    [
        ("mode", None),
        ("segments", ["not-a-record"]),
        ("segments", [{}]),
        ("segments", [{**SEGMENT, "label": None}]),
        ("segments", [{**SEGMENT, "from": "private-fixture-value"}]),
        ("segments", [{**SEGMENT, "from_dt": 7}]),
        ("segments", [{**SEGMENT, "from_dt": "private-fixture-value"}]),
        ("segments", [{**SEGMENT, "to_dt": "2024-06-10T00:00:00"}]),
        ("conflicts", ["not-a-record"]),
        ("conflicts", [{}]),
        ("conflicts", [{"trip1": "A", "trip2": "B", "overlap": "private-fixture-value"}]),
        ("ooo", {}),
        ("ooo", {"created": True, "deleted": 0, "setToP2": 0, "prioritySet": 0}),
        ("ooo", {"created": 0, "deleted": 0, "setToP2": 1, "prioritySet": 0}),
        (
            "ooo",
            {
                "created": 0,
                "deleted": 0,
                "setToP2": 0,
                "prioritySet": 0,
                "createdNames": "not-an-array",
                "deletedNames": [],
            },
        ),
        ("timezoneChanges", [{"action": "create"}]),
    ],
)
def test_cli_refuses_incomplete_nested_success_records(key, value):
    payload = successful_payload()
    payload[key] = value
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(payload), text=True, capture_output=True
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Inspect the sync JSON" in result.stderr
    assert "private-fixture-value" not in result.stderr


def test_cli_accepts_complete_success_records_without_changing_the_schedule():
    payload = successful_payload()
    payload["conflicts"] = [{"trip1": "A", "trip2": "B", "overlap": "2024-06-09"}]
    payload["ooo"] = {
        "created": 0,
        "deleted": 0,
        "setToP2": 0,
        "prioritySet": 0,
        "createdNames": [],
        "deletedNames": [],
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(payload), text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["segments"] == payload["segments"]


@pytest.mark.parametrize(
    "zone,instant,active",
    [
        ("America/Chicago", "2024-06-10T04:59:00+00:00", True),
        ("America/Chicago", "2024-06-10T05:00:00+00:00", False),
        ("Europe/Brussels", "2024-06-09T21:59:00+00:00", True),
        ("Europe/Brussels", "2024-06-09T22:00:00+00:00", False),
        ("invalid-zone", "2024-06-10T00:00:00+00:00", False),
        (None, "2024-06-10T00:00:00+00:00", False),
    ],
)
def test_cli_report_uses_the_same_home_date_as_reclaim(zone, instant, active, monkeypatch, capsys):
    reference = datetime.fromisoformat(instant)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=UTC):
            return reference.astimezone(tz)

    payload = {
        "errors": [],
        "mode": "sync",
        "noChanges": False,
        "homeTimezone": zone,
        "segments": [],
        "conflicts": [],
        "ooo": None,
        "timezoneChanges": [change(start="2024-06-08", end="2024-06-10")],
    }
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module.sys, "stdin", StringIO(json.dumps(payload)))
    assert module.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert bool(output["timezoneDiff"]["added"]) is active
    assert bool(output["reportLines"]) is active
