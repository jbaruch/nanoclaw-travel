"""Keep #384's compact rules bound to their shipped skill contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / ".tessl-plugin/plugin.json").read_text())
MOVED_CONTRACTS = [
    ("operator-local-tz-phrasing", "flight-assist"),
    ("travel-schedule-contract", "nightly-travel-sync"),
]


@pytest.mark.parametrize("rule", MANIFEST["rules"])
def test_loaded_travel_rules_stay_within_two_kib(rule):
    assert (ROOT / rule).stat().st_size <= 2048


@pytest.mark.parametrize(("name", "skill"), MOVED_CONTRACTS)
def test_moved_contract_has_a_declared_rule_and_native_read_gate(name, skill):
    rule_path = f"rules/{name}.md"
    assert rule_path in MANIFEST["rules"]
    assert f"]({rule_path})" in (ROOT / "README.md").read_text()
    rule = (ROOT / rule_path).read_text()
    native = f"/home/node/.claude/skills/tessl__{skill}/references/{name}.md"
    assert f"read `{native}` in full" in rule
    assert "Its full text binds as rule content" in rule
    assert "Missing or unreadable content stops" in rule
    assert "never `@`-import the reference" in rule
    assert "alwaysApply: false" in rule
    assert "applyTo:" in rule
    if skill == "flight-assist":
        assert "Before authoring, modifying or composing" in rule
        assert native in (ROOT / "skills/flight-assist/SKILL.md").read_text()


@pytest.mark.parametrize(("name", "skill"), MOVED_CONTRACTS)
def test_moved_contract_citations_resolve_to_declared_skill_content(name, skill):
    reference = ROOT / f"skills/{skill}/references/{name}.md"
    assert reference.is_file()
    # Native paths resolve to separately mounted declared skills, not a
    # repository-relative skills/ tree underneath another installed skill.
    content = (ROOT / f"rules/{name}.md").read_text() + reference.read_text()
    native_paths = re.findall(r"/home/node/\.claude/skills/tessl__([\w-]+)/([\w./-]+)", content)
    assert native_paths
    for owner, relative in native_paths:
        if owner == "current-tz":
            continue  # External nanoclaw-core reader, covered by test_operator_tz.
        assert f"skills/{owner}" in MANIFEST["skills"]
        assert (ROOT / "skills" / owner / relative).is_file(), relative
    for citation in re.findall(r"`((?:skills|rules)/[\w./-]+\.(?:md|py))`", content):
        assert (ROOT / citation).is_file(), citation


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
    rule = (ROOT / "rules/operator-local-tz-phrasing.md").read_text()
    assert "never read its backing store directly" in rule
    assert "Never derive relative-date wording from the container clock" in rule
    contract = (ROOT / "skills/flight-assist/references/operator-local-tz-phrasing.md").read_text()
    assert "## Script-computed labels" in contract
    assert "at most once per cycle" in contract
    assert "This script conversion is permitted; model conversion is forbidden" in contract
    assert "render the label verbatim, never re-derive it" in contract
    assert "departure's supplied airport-local offset" in contract
