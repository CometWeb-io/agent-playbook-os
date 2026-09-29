from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.run_comparison import (
    ComparisonConfig,
    assert_compatible,
    build_comparison_record,
    load_case,
)


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "benchmarks" / "cases" / "evidence-to-decision.yaml"


def test_comparison_record_contains_both_paths_and_hashes():
    record = build_comparison_record(load_case(CASE), dry_run=True)

    assert set(record["paths"]) == {"legacy_skill_orchestrator", "agent_playbook_os"}
    playbook = record["paths"]["agent_playbook_os"]
    assert playbook["playbook_hash"].startswith("sha256:")
    assert playbook["semantic_hash"].startswith("sha256:")
    assert all(value.startswith("sha256:") for value in playbook["skill_hashes"].values())
    assert record["assessment"] == "structural-only"


def test_harness_rejects_mismatched_skill_or_model_configuration():
    base = ComparisonConfig("sha256:skills", "reference", "dry", "sha256:model")
    changed = ComparisonConfig("sha256:other", "reference", "dry", "sha256:model")

    with pytest.raises(ValueError, match="same skill library"):
        assert_compatible(base, changed)


def test_harness_redacts_secret_inputs_and_provider_credentials():
    case = load_case(CASE)
    case["inputs"] = {"question": "Bearer super-secret"}
    case["provider_credentials"] = {"client_secret": "provider-secret"}

    record = build_comparison_record(case, dry_run=True)
    serialized = str(record)

    assert "super-secret" not in serialized
    assert "provider-secret" not in serialized
