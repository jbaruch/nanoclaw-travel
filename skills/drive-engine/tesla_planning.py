"""Read-only adapter to the declared, installed nanoclaw-tesla provider.

Only its cached state CLI and estimate CLI are executable targets. Never call
poll, control, HTTP, enroll or replace native identity. Provider failures remain
observable and return no estimate; they never select a different data route.
"""

from __future__ import annotations

import json
import os
import selectors
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ev_charging import EnergySample, Navigation, finite

MOUNT = Path("/home/node/.claude/skills")
CLONE_PROVIDER = Path(__file__).resolve().parents[2] / ".tessl/plugins/jbaruch/nanoclaw-tesla"
MAX_OUTPUT = 1024 * 1024
TIMEOUT = 225


class PlanningUnavailable(ValueError):
    """Install/inspect the provider and owner selection; retain legacy drive timing."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanningUnavailable("Duplicate provider JSON field.")
        result[key] = value
    return result


def run_json(argv: list[str]) -> dict:
    """Bound both pipes without buffering unbounded provider output in memory."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        assert process.stdout is not None and process.stderr is not None
        output = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
        deadline = time.monotonic() + TIMEOUT
        try:
            with selectors.DefaultSelector() as selector:
                for descriptor in output:
                    selector.register(descriptor, selectors.EVENT_READ)
                while selector.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise PlanningUnavailable("Tesla planning read exceeded its bounded wait.")
                    for key, _ in selector.select(min(left, 1)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        data = output[key.fd]
                        data.extend(chunk)
                        if len(data) > MAX_OUTPUT:
                            raise PlanningUnavailable("Tesla planning output exceeded its bound.")
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise PlanningUnavailable("Tesla planning provider refused this read.")
        except (OSError, subprocess.TimeoutExpired, PlanningUnavailable):
            # Only this adapter's read-only child. Never signal SDK/browser/provider daemons.
            process.kill()
            process.wait()
            raise PlanningUnavailable(
                "Tesla planning read unavailable; no alternate route."
            ) from None
        try:
            value = json.loads(
                output[process.stdout.fileno()].decode("utf-8"), object_pairs_hook=_unique
            )
        except (ValueError, UnicodeError, RecursionError):
            raise PlanningUnavailable("Tesla planning provider returned invalid JSON.") from None
        if not isinstance(value, dict):
            raise PlanningUnavailable("Tesla planning provider returned no object.")
        return value


def _regular(path: Path, root: Path) -> Path:
    # The fixed provider root and every named component remain regular/contained.
    # Never resolve a target through an installation or skill-directory symlink.
    relative = path.relative_to(root)
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise PlanningUnavailable("Reinstall the complete regular Tesla provider.")
    candidate = root
    if not stat.S_ISDIR(candidate.lstat().st_mode):
        raise PlanningUnavailable("Reinstall the complete regular Tesla provider.")
    for index, part in enumerate(relative.parts):
        candidate = candidate / part
        mode = candidate.lstat().st_mode
        expected = stat.S_ISREG if index == len(relative.parts) - 1 else stat.S_ISDIR
        if not expected(mode):
            raise PlanningUnavailable("Reinstall the complete regular Tesla provider.")
    return path


def provider_scripts() -> tuple[Path, Path]:
    ops, charge = MOUNT / "tessl__tessie-ops/scripts", MOUNT / "tessl__tessie-charge/scripts"
    root = MOUNT
    if not (os.path.lexists(ops.parent) or os.path.lexists(charge.parent)):
        # Finite declared dependency path in this verified travel source clone.
        owner = Path(__file__).absolute().parent
        if owner.name != "drive-engine" or owner.parent.name != "skills":
            raise PlanningUnavailable("Install both native Tesla provider skill mounts.")
        root = CLONE_PROVIDER
        try:
            manifest = json.loads(
                _regular(root / ".tessl-plugin/plugin.json", root).read_text("utf-8")
            )
        except RecursionError:
            raise PlanningUnavailable("Reinstall the bounded Tesla provider manifest.") from None
        if not isinstance(manifest, dict) or manifest.get("name") != "jbaruch/nanoclaw-tesla":
            raise PlanningUnavailable("Install the declared nanoclaw-tesla provider.")
        ops, charge = (
            CLONE_PROVIDER / "skills/tessie-ops/scripts",
            CLONE_PROVIDER / "skills/tessie-charge/scripts",
        )
    # Validate complete named direct transport closure before either CLI executes.
    for directory, names in (
        (
            ops,
            (
                "tessie-read.py",
                "tessie_cli.py",
                "tessie_core.py",
                "tessie_actions.py",
                "tessie_policy.py",
                "tessie_native.py",
            ),
        ),
        (charge, ("tessie-charge.py", "charge_ipc.py")),
    ):
        for name in names:
            _regular(directory / name, root)
    return ops / "tessie-read.py", charge / "tessie-charge.py"


class TeslaPlanning:
    def __init__(self, *, now: datetime, run=run_json, configured=False):
        self.now = now
        self.run = run
        self.states: dict[str | None, dict | None] = {}
        self.samples: dict[tuple[str, float, datetime], EnergySample | None] = {}
        self.diagnostics: set[str] = set()
        self.paths: tuple[Path, Path] | None = None
        try:
            self.paths = provider_scripts()
        except (OSError, ValueError):
            if configured:
                self.unavailable()

    def unavailable(self):
        message = (
            "[drive-engine] EV planning unavailable: install the compatible Tesla charging "
            "provider and inspect native owner enrollment/exact vehicle selection. "
            "Legacy drive timing retained; no vehicle control or alternate provider route."
        )
        if message not in self.diagnostics:
            self.diagnostics.add(message)
            print(message, file=sys.stderr)

    def state(self, selector: str | None) -> dict | None:
        if selector in self.states:
            return self.states[selector]
        self.states[selector] = None
        if self.paths is None:
            return None
        argv = [sys.executable, str(self.paths[0]), "state", "--include-location"]
        if selector is not None:
            argv += [f"--vehicle={selector}"]
        try:
            value = self.run(argv)
            vehicle = value.get("vehicle")
            alias = vehicle.get("alias") if isinstance(vehicle, dict) else None
            if (
                value.get("kind") != "state"
                or not isinstance(alias, str)
                or not alias.strip()
                or len(alias) > 100
                or not isinstance(value.get("data"), dict)
            ):
                raise PlanningUnavailable("Inspect the native selected vehicle state.")
            self.states[selector] = value
        except (OSError, ValueError):
            self.unavailable()
        return self.states[selector]

    def vehicle(self, selector: str | None) -> str | None:
        value = self.state(selector)
        return value["vehicle"]["alias"] if value is not None else None

    def estimate(self, vehicle: str, distance: float, departure: datetime) -> EnergySample | None:
        key = (vehicle, distance, departure)
        if key in self.samples:
            return self.samples[key]
        self.samples[key] = None
        if self.paths is None:
            return None
        try:
            data = self.run(
                [
                    sys.executable,
                    str(self.paths[1]),
                    "estimate",
                    f"--vehicle={vehicle}",
                    "--distance-km",
                    str(distance),
                    "--departure",
                    departure.isoformat(),
                ]
            )
            if (
                type(data.get("schema_version")) is not int
                or data["schema_version"] != 1
                or data.get("forecast_only") is not True
                or data.get("vehicle") != vehicle
                or not isinstance(data.get("measured_at"), str)
            ):
                raise PlanningUnavailable("Inspect the native charging estimate contract.")
            sample = EnergySample(
                vehicle,
                data.get("usable_capacity_kwh"),
                data.get("measured_kwh_per_km"),
                datetime.fromisoformat(data["measured_at"].replace("Z", "+00:00")),
                data.get("expected_departure_percent"),
            )
            if not sample.valid(vehicle, self.now):
                raise PlanningUnavailable("Inspect complete recent native energy inputs.")
            self.samples[key] = sample
        except (OSError, ValueError, TypeError):
            self.unavailable()
        return self.samples[key]

    def navigation(self) -> list[Navigation]:
        result = []
        seen = set()
        for value in self.states.values():
            if value is None:
                continue
            alias = value["vehicle"]["alias"]
            if alias in seen:
                continue
            seen.add(alias)
            data = value["data"].get("drive_state")
            if not isinstance(data, dict):
                continue
            stamp = data.get("timestamp")
            destination = data.get("active_route_destination")
            minutes = data.get("active_route_minutes_to_arrival")
            arrival = data.get("active_route_energy_at_arrival")
            if (
                not isinstance(stamp, (int, float))
                or not isinstance(minutes, (int, float))
                or not isinstance(arrival, (int, float))
                or not finite(stamp, 0, 10**15)
                or not finite(minutes, 0.01, 1440)
                or not finite(arrival, 0, 100)
                or not isinstance(destination, str)
            ):
                continue
            try:
                result.append(
                    Navigation(
                        alias,
                        destination,
                        datetime.fromtimestamp(stamp / 1000, UTC),
                        minutes,
                        arrival,
                    )
                )
            except (OverflowError, ValueError):
                continue
        return result
