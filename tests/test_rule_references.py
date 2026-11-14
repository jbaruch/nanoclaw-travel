"""Keep #384's compact rules bound to their shipped skill contracts."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / ".tessl-plugin/plugin.json").read_text())
MOVED_CONTRACTS = [
    ("operator-local-tz-phrasing", "flight-assist"),
    ("travel-schedule-contract", "nightly-travel-sync"),
]


@pytest.fixture
def installed_plugin(tmp_path):
    """Mirror the host's native skill mounts and materialized startup rules.

    Layout: jbaruch/nanoclaw ef8cf4d, src/volume-mounts.ts:715-759, 830-834.
    Native Read-tool acceptance: tests/README.md.
    """
    native = tmp_path / "home/node/.claude"
    for skill in MANIFEST["skills"]:
        shutil.copytree(ROOT / skill, native / "skills" / f"tessl__{Path(skill).name}")
    rules = native / ".tessl/RULES.md"
    rules.parent.mkdir()
    rules.write_text("\n\n---\n\n".join((ROOT / rule).read_text() for rule in MANIFEST["rules"]))
    return tmp_path, native, rules


@pytest.mark.parametrize("rule", MANIFEST["rules"])
def test_loaded_travel_rules_stay_within_two_kb(rule):
    assert (ROOT / rule).stat().st_size <= 2000


def test_schedule_contract_has_a_declared_rule_and_native_read_gate(installed_plugin):
    _, installed, startup = installed_plugin
    rule_path = "rules/travel-schedule-contract.md"
    assert rule_path in MANIFEST["rules"]
    assert f"]({rule_path})" in (ROOT / "README.md").read_text()
    rule = (ROOT / rule_path).read_text()
    native = (
        "/home/node/.claude/skills/tessl__nightly-travel-sync/"
        "references/travel-schedule-contract.md"
    )
    assert rule in startup.read_text()
    assert f"read `{native}` in full" in rule
    assert "Its full text binds as rule content" in rule
    assert "Missing or unreadable content stops" in rule
    assert "never `@`-import the reference" in rule
    assert "alwaysApply: false" in rule
    assert "applyTo:" in rule


def test_timezone_contract_is_skill_scoped_without_a_duplicate_startup_rule(installed_plugin):
    _, installed, startup = installed_plugin
    assert "rules/operator-local-tz-phrasing.md" not in MANIFEST["rules"]
    assert not (ROOT / "rules/operator-local-tz-phrasing.md").exists()
    assert "Operator-Local Timezone Phrasing" not in startup.read_text()
    skill = (installed / "skills/tessl__flight-assist/SKILL.md").read_text()
    native = (
        "/home/node/.claude/skills/tessl__flight-assist/references/operator-local-tz-phrasing.md"
    )
    assert native in skill
    assert "in full. Missing or unreadable content stops" in skill


@pytest.mark.parametrize(("name", "skill"), MOVED_CONTRACTS)
def test_moved_contract_citations_resolve_to_installed_skill_content(installed_plugin, name, skill):
    root, installed, startup = installed_plugin
    reference = installed / f"skills/tessl__{skill}/references/{name}.md"
    assert reference.is_file()
    # Native paths resolve to separately mounted declared skills, not a
    # repository-relative skills/ tree underneath another installed skill.
    content = startup.read_text() + reference.read_text()
    native_paths = re.findall(r"/home/node/\.claude/skills/tessl__([\w-]+)/([\w./-]+)", content)
    assert native_paths
    for owner, relative in native_paths:
        if owner == "current-tz":
            continue  # External nanoclaw-core reader, covered by test_operator_tz.
        assert f"skills/{owner}" in MANIFEST["skills"]
        native = f"/home/node/.claude/skills/tessl__{owner}/{relative}"
        assert (root / native.lstrip("/")).is_file(), native
    for citation in re.findall(r"`((?:skills|rules)/[\w./-]+\.(?:md|py))`", content):
        if citation.startswith("skills/"):
            _, owner, relative = citation.split("/", 2)
            assert (installed / "skills" / f"tessl__{owner}" / relative).is_file(), citation
        else:
            assert citation in MANIFEST["rules"], citation


@pytest.mark.parametrize(
    ("name", "skill", "digest"),
    [
        (
            "operator-local-tz-phrasing",
            "flight-assist",
            "dcda6fa5f419d7c92517a7346a46ffbb795a9fc5f3911f4fa8e00de8f5c11e3b",
        ),
        (
            "travel-schedule-contract",
            "nightly-travel-sync",
            "c67ea1175d19b0b4dc6ff3b8e9d53a22cce38255a43c71203f88f3ad6509d60d",
        ),
    ],
)
def test_installed_references_retain_complete_accepted_contracts(
    installed_plugin, name, skill, digest
):
    # Independent full-body fixtures from e23553a, including the accepted
    # script-conversion clarification and removal of the expired rollout.
    _, installed, _ = installed_plugin
    reference = installed / f"skills/tessl__{skill}/references/{name}.md"
    assert hashlib.sha256(reference.read_bytes()).hexdigest() == digest


def test_schedule_boundary_stays_loaded_and_rollout_exception_is_removed():
    rule = (ROOT / "rules/travel-schedule-contract.md").read_text()
    assert "never migrates or writes" in rule
    assert "untrusted data" in rule
    assert "sanitize it before model output" in rule
    assert "does not prove a flight was taken" in rule
    reference = ROOT / "skills/nightly-travel-sync/references/travel-schedule-contract.md"
    schema = ROOT / "skills/nightly-travel-sync/state-schema.md"
    for text in [rule, reference.read_text(), schema.read_text()]:
        assert "integer record version 5" in text
        assert "2026-10-28T00:00:00Z" not in text
        assert "initial rollout" not in text.lower()
        assert "initial-rollout" not in text.lower()


def test_timezone_reference_distinguishes_script_conversion_from_model_composition():
    contract = (ROOT / "skills/flight-assist/references/operator-local-tz-phrasing.md").read_text()
    assert "## Script-computed labels" in contract
    assert "at most once per cycle" in contract
    assert "This script conversion is permitted; model conversion is forbidden" in contract
    assert "render the label verbatim, never re-derive it" in contract
    assert "departure's supplied airport-local offset" in contract
