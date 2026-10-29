"""Installed-provider boundaries and real bounded synthetic CLI processes only."""

from __future__ import annotations

import json
import runpy
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/drive-engine"))
import tesla_planning as owner  # noqa: E402

NOW = datetime(2020, 7, 13, tzinfo=UTC)


def provider(tmp_path, monkeypatch):
    mount = tmp_path / "skills"
    for skill, files in (
        (
            "tessie-ops",
            (
                "tessie-read.py",
                "tessie_cli.py",
                "tessie_core.py",
                "tessie_actions.py",
                "tessie_policy.py",
                "tessie_native.py",
            ),
        ),
        ("tessie-charge", ("tessie-charge.py", "charge_ipc.py")),
    ):
        directory = mount / f"tessl__{skill}" / "scripts"
        directory.mkdir(parents=True)
        for name in files:
            (directory / name).write_text("# synthetic provider target\n")
    monkeypatch.setattr(owner, "MOUNT", mount)
    return mount


def state(alias="Car"):
    return {
        "kind": "state",
        "vehicle": {"alias": alias},
        "data": {
            "drive_state": {
                "timestamp": NOW.timestamp() * 1000,
                "active_route_destination": "Venue",
                "active_route_minutes_to_arrival": 123,
                "active_route_energy_at_arrival": 17,
            }
        },
    }


def sample(**changes):
    return {
        "schema_version": 1,
        "vehicle": "Car",
        "usable_capacity_kwh": 75,
        "measured_kwh_per_km": 0.18,
        "measured_at": NOW.isoformat(),
        "expected_departure_percent": 90,
        "estimate": {},
        "forecast_only": True,
        **changes,
    }


def test_actual_cli_json_runner_and_nonzero_refusal(tmp_path):
    script = tmp_path / "fixture.py"
    script.write_text("import json; print(json.dumps({'schema_version': 1}))\n")
    assert owner.run_json([sys.executable, str(script)]) == {"schema_version": 1}
    script.write_text("print('private provider error'); raise SystemExit(4)\n")
    with pytest.raises(owner.PlanningUnavailable, match="unavailable"):
        owner.run_json([sys.executable, str(script)])


@pytest.mark.parametrize(
    "body",
    [
        "print('[]')",
        "print('not JSON')",
        'print(\'{"a":1,"a":2}\')',
        "import os; os.write(1,b'\\xff')",
    ],
)
def test_real_process_invalid_output_never_supplies_estimate(tmp_path, body):
    script = tmp_path / "fixture.py"
    script.write_text(body)
    with pytest.raises(owner.PlanningUnavailable):
        owner.run_json([sys.executable, str(script)])


def test_real_child_oversized_output_is_bounded_and_retired(tmp_path, monkeypatch):
    monkeypatch.setattr(owner, "MAX_OUTPUT", 1024)
    script = tmp_path / "fixture.py"
    script.write_text("import os; os.write(1, b'x'*65536)\n")
    with pytest.raises(owner.PlanningUnavailable):
        owner.run_json([sys.executable, str(script)])


def test_fixed_readonly_argv_and_one_sample_per_identical_request(tmp_path, monkeypatch):
    provider(tmp_path, monkeypatch)
    calls = []

    def run(argv):
        calls.append(argv)
        return state() if argv[2] == "state" else sample()

    api = owner.TeslaPlanning(now=NOW, run=run)
    assert api.vehicle("Car; literal") == "Car"
    departure = NOW + timedelta(hours=2)
    measured = api.estimate("Car", 395, departure)
    assert measured is not None and measured.capacity_kwh == 75
    assert api.estimate("Car", 395, departure) is measured
    assert len(calls) == 2
    assert calls[0][2:] == ["state", "--include-location", "--vehicle=Car; literal"]
    assert calls[1][2:] == [
        "estimate",
        "--vehicle=Car",
        "--distance-km",
        "395",
        "--departure",
        departure.isoformat(),
    ]
    assert api.navigation()[0].vehicle == "Car"


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"vehicle": "Other"},
        {"forecast_only": False},
        {"expected_departure_percent": None},
        {"usable_capacity_kwh": 0},
        {"measured_kwh_per_km": float("nan")},
        {"measured_at": "unknown"},
    ],
)
def test_bad_estimate_no_cache_admission_and_fixed_observable_hold(
    tmp_path, monkeypatch, capsys, changes
):
    provider(tmp_path, monkeypatch)
    calls = []

    def run(argv):
        calls.append(argv)
        return sample(**changes)

    api = owner.TeslaPlanning(now=NOW, run=run)
    assert api.estimate("Car", 395, NOW) is None
    assert api.estimate("Car", 395, NOW) is None
    assert len(calls) == 1  # absent sample memoized; no retry into another answer
    diagnostic = capsys.readouterr().err
    assert "Legacy drive timing retained" in diagnostic
    assert "nan" not in diagnostic and "unknown" not in diagnostic


def test_partial_native_mount_never_falls_back_to_a_different_install(tmp_path, monkeypatch):
    mount = provider(tmp_path, monkeypatch)
    (mount / "tessl__tessie-charge/scripts/charge_ipc.py").unlink()
    called = []
    api = owner.TeslaPlanning(now=NOW, configured=True, run=lambda argv: called.append(argv))
    assert api.vehicle("Car") is None
    assert called == []


def test_linked_provider_target_refused_before_execution(tmp_path, monkeypatch):
    mount = provider(tmp_path, monkeypatch)
    target = mount / "tessl__tessie-charge/scripts/charge_ipc.py"
    target.unlink()
    target.symlink_to(mount / "tessl__tessie-ops/scripts/tessie_native.py")
    with pytest.raises(owner.PlanningUnavailable):
        owner.provider_scripts()


def test_ambiguous_vehicle_refusal_does_not_select_another_vehicle(tmp_path, monkeypatch):
    provider(tmp_path, monkeypatch)
    calls = []

    def run(argv):
        calls.append(argv)
        raise owner.PlanningUnavailable("synthetic ambiguous fleet")

    api = owner.TeslaPlanning(now=NOW, run=run)
    assert api.vehicle(None) is None
    assert api.vehicle(None) is None
    assert len(calls) == 1


def test_normal_source_clone_manifest_identity_and_complete_closure(tmp_path, monkeypatch):
    mount = provider(tmp_path, monkeypatch)
    clone = tmp_path / "plugin"
    (clone / ".tessl-plugin").mkdir(parents=True)
    (clone / ".tessl-plugin/plugin.json").write_text(json.dumps({"name": "jbaruch/nanoclaw-tesla"}))
    (clone / "skills").mkdir()
    for name in ("tessie-ops", "tessie-charge"):
        (mount / f"tessl__{name}").rename(clone / "skills" / name)
    monkeypatch.setattr(owner, "CLONE_PROVIDER", clone)
    assert owner.provider_scripts()[1] == clone / "skills/tessie-charge/scripts/tessie-charge.py"
    (clone / ".tessl-plugin/plugin.json").write_text(json.dumps({"name": "impostor"}))
    with pytest.raises(owner.PlanningUnavailable):
        owner.provider_scripts()


def test_horizon_and_unrelated_charge_rows_do_not_start_extra_provider_reads(monkeypatch):
    from dataclasses import replace

    sys.path.insert(0, str(ROOT / "skills/flight-assist"))
    from ev_charging import EnergySample
    from reconcile import DesiredBlock
    from reconcile_sweep import _ev_adjuster

    calls = []

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, selector):
            calls.append(selector)
            return "Car"

        def estimate(self, *_args):
            return EnergySample("Car", 75, 0.18, NOW, 90)

        def navigation(self):
            return []

        def unavailable(self):
            raise AssertionError("Complete near-term synthetic inputs must remain usable")

    monkeypatch.setattr(owner, "TeslaPlanning", Provider)
    block = DesiredBlock(
        "meeting",
        "meeting_outbound",
        "Drive",
        NOW + timedelta(hours=9),
        NOW + timedelta(hours=12),
        "Home",
        "Venue",
        10800,
        NOW + timedelta(hours=12),
    )
    far = replace(
        block,
        identity="far-flight",
        kind="airport_departure",
        start=NOW + timedelta(days=30),
        end=NOW + timedelta(days=30, hours=3),
    )
    preferences = {
        "schema_version": 1,
        "stop_minutes": 30,
        "reserve_percent": 10,
        "cold_allowance_percent": 0,
        "highway_allowance_percent": 0,
        "default_vehicle": "Car",
        "event_vehicles": {},
        "person_vehicles": {},
        "event_drivers": {},
        "destination_charges": [
            {
                "schema_version": 1,
                "vehicle": "Irrelevant",
                "destination": "Other venue",
                "available_at": (NOW + timedelta(hours=10)).isoformat(),
                "energy_kwh": 20,
                "source": "Unrelated grounded owner message",
            }
        ],
    }
    adjust = _ev_adjuster(
        {"ev_planning": preferences}, lambda *_: None, {("Home", "Venue"): 395}, NOW
    )
    result, held = adjust([block, far], [], [])
    assert held == []
    assert result[0].start == block.start - timedelta(minutes=30)
    assert result[1] is far
    assert calls == ["Car"]


def test_installed_prefixed_consumer_never_uses_a_source_clone_provider(tmp_path, monkeypatch):
    # Execute the actual co-shipped adapter from its native prefixed layout.
    # A valid-looking source provider elsewhere must not replace missing mounts.
    mount = provider(tmp_path, monkeypatch)
    native = tmp_path / "native/skills/tessl__drive-engine"
    native.mkdir(parents=True)
    target = native / "tesla_planning.py"
    target.write_bytes((ROOT / "skills/drive-engine/tesla_planning.py").read_bytes())
    module = runpy.run_path(str(target))
    clone = tmp_path / "substitute"
    (clone / ".tessl-plugin").mkdir(parents=True)
    (clone / ".tessl-plugin/plugin.json").write_text(json.dumps({"name": "jbaruch/nanoclaw-tesla"}))
    (clone / "skills").mkdir()
    for name in ("tessie-ops", "tessie-charge"):
        (mount / f"tessl__{name}").rename(clone / "skills" / name)
    # The function's globals are the actually loaded module dictionary.
    lookup = module["provider_scripts"]
    lookup.__globals__["MOUNT"] = tmp_path / "absent-native"
    lookup.__globals__["CLONE_PROVIDER"] = clone
    with pytest.raises(module["PlanningUnavailable"], match="both native"):
        lookup()


@pytest.mark.parametrize("component", ["tessl__tessie-ops", "tessl__tessie-charge"])
def test_linked_native_skill_ancestor_refused_before_execution(tmp_path, monkeypatch, component):
    mount = provider(tmp_path, monkeypatch)
    original = mount / component
    substitute = tmp_path / "substitute"
    original.rename(substitute)
    original.symlink_to(substitute, target_is_directory=True)
    called = []
    api = owner.TeslaPlanning(now=NOW, configured=True, run=lambda argv: called.append(argv))
    assert api.vehicle("Car") is None
    assert called == []


@pytest.mark.parametrize(
    "component", ["skills", "skills/tessie-ops", "skills/tessie-charge", ".tessl-plugin"]
)
def test_linked_source_provider_ancestor_refused(tmp_path, monkeypatch, component):
    mount = provider(tmp_path, monkeypatch)
    clone = tmp_path / "plugin"
    (clone / ".tessl-plugin").mkdir(parents=True)
    (clone / ".tessl-plugin/plugin.json").write_text(json.dumps({"name": "jbaruch/nanoclaw-tesla"}))
    (clone / "skills").mkdir()
    for name in ("tessie-ops", "tessie-charge"):
        (mount / f"tessl__{name}").rename(clone / "skills" / name)
    monkeypatch.setattr(owner, "CLONE_PROVIDER", clone)
    original = clone / component
    substitute = tmp_path / "substitute"
    original.rename(substitute)
    original.symlink_to(substitute, target_is_directory=True)
    with pytest.raises(owner.PlanningUnavailable):
        owner.provider_scripts()


@pytest.mark.parametrize("reason", ["selector_conflict", "unknown_driver", "unresolved_provider"])
def test_middle_ambiguous_drive_holds_downstream_selected_vehicle(monkeypatch, reason):
    from dataclasses import replace

    from ev_charging import EnergySample
    from reconcile import DesiredBlock
    from reconcile_sweep import _ev_adjuster

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def vehicle(self, selector):
            return None if selector == "Unresolved" else "Car"

        def estimate(self, vehicle, *_args):
            return EnergySample(vehicle, 75, 0.18, NOW, 90)

        def navigation(self):
            return []

        def unavailable(self):
            pass

    monkeypatch.setattr(owner, "TeslaPlanning", Provider)
    first = DesiredBlock(
        "first",
        "meeting_outbound",
        "Drive",
        NOW + timedelta(hours=9),
        NOW + timedelta(hours=12),
        "Home",
        "Venue",
        10800,
        NOW + timedelta(hours=12),
    )
    middle = replace(
        first, identity="middle", start=NOW + timedelta(hours=13), end=NOW + timedelta(hours=16)
    )
    later = replace(
        first, identity="later", start=NOW + timedelta(hours=17), end=NOW + timedelta(hours=20)
    )
    preferences = {
        "schema_version": 1,
        "stop_minutes": 30,
        "reserve_percent": 10,
        "cold_allowance_percent": 0,
        "highway_allowance_percent": 0,
        "default_vehicle": "Car",
        "event_vehicles": {},
        "person_vehicles": {},
        "event_drivers": {},
        "destination_charges": [],
    }
    if reason == "selector_conflict":
        preferences.update(
            event_vehicles={"middle": "Car"},
            person_vehicles={"Driver": "Other"},
            event_drivers={"middle": "Driver"},
        )
    elif reason == "unknown_driver":
        preferences["event_drivers"] = {"middle": "Unknown"}
    else:
        preferences["event_vehicles"] = {"middle": "Unresolved"}
    adjust = _ev_adjuster(
        {"ev_planning": preferences}, lambda *_: None, {("Home", "Venue"): 395}, NOW
    )
    result, held = adjust([first, middle, later], [], [])
    assert held == []
    assert result[0].start == first.start - timedelta(minutes=30)
    assert result[1] is middle and result[2] is later
    from block_codec import parse_block
    from calendar_apply import build_create_args

    retained = replace(later, charging_note="1 charging stop, ~30 min", baseline_seconds=12600)
    current = parse_block(
        {"id": "owned-later", **build_create_args(retained, calendar_id="primary")}
    )
    assert current is not None
    result, held = adjust([first, middle, later], [], [current])
    assert later not in result
    assert [(item.identity, item.kind, item.announce) for item in held] == [
        ("later", "meeting_outbound", False)
    ]


@pytest.mark.parametrize("manifest", [None, [], 1, "not-an-object", True])
def test_nonobject_declared_provider_manifest_holds_before_execution(
    tmp_path, monkeypatch, manifest, capsys
):
    mount = provider(tmp_path, monkeypatch)
    clone = tmp_path / "plugin"
    (clone / ".tessl-plugin").mkdir(parents=True)
    (clone / ".tessl-plugin/plugin.json").write_text(json.dumps(manifest))
    for name in ("tessie-ops", "tessie-charge"):
        (mount / f"tessl__{name}").rename(tmp_path / name)
    monkeypatch.setattr(owner, "CLONE_PROVIDER", clone)
    calls = []
    api = owner.TeslaPlanning(now=NOW, configured=True, run=lambda argv: calls.append(argv))
    assert api.vehicle("Car") is None and calls == []
    assert "Legacy drive timing retained" in capsys.readouterr().err
