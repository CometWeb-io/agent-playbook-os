#!/usr/bin/env python3
"""Write an honest, redacted legacy-vs-playbook comparison record."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_playbook_os.compiler import Compiler  # noqa: E402
from agent_playbook_os.loader import load_playbook  # noqa: E402
from agent_playbook_os.telemetry import redact  # noqa: E402


@dataclass(frozen=True)
class ComparisonConfig:
    skill_library_hash: str
    provider: str
    model: str
    model_config_hash: str


def load_case(path: str | Path) -> dict[str, Any]:
    case_path = Path(path).resolve()
    raw = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("comparison case must be a mapping")
    for key in ("id", "playbook", "inputs", "skill_library", "model"):
        if key not in raw:
            raise ValueError(f"comparison case missing {key}")
    raw["_path"] = str(case_path)
    return raw


def _config(raw: Mapping[str, Any]) -> ComparisonConfig:
    library = raw["skill_library"]
    model = raw["model"]
    if not isinstance(library, Mapping) or not isinstance(model, Mapping):
        raise ValueError("skill_library and model must be mappings")
    values = ComparisonConfig(
        skill_library_hash=str(library["hash"]),
        provider=str(model["provider"]),
        model=str(model["name"]),
        model_config_hash=str(model["config_hash"]),
    )
    for name, value in (
        ("skill library hash", values.skill_library_hash),
        ("model config hash", values.model_config_hash),
    ):
        if not value.startswith("sha256:"):
            raise ValueError(f"{name} must be a sha256: digest")
    return values


def assert_compatible(legacy: ComparisonConfig, playbook: ComparisonConfig) -> None:
    if legacy != playbook:
        raise ValueError(
            "comparison paths must use the same skill library and model configuration"
        )


def _resolve_playbook(case: Mapping[str, Any]) -> Path:
    case_path = Path(str(case["_path"])).parent
    path = (case_path / str(case["playbook"])).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("comparison playbook must remain inside the repository")
    return path


def _usage_for_plan(plan) -> dict[str, int | float]:
    return {
        "model_calls": 0,
        "tool_calls": 0,
        "skill_calls": sum(step.spec.type == "skill" for step in plan.steps),
        "action_calls": sum(step.spec.type == "action" for step in plan.steps),
        "retries": 0,
        "wall_time_seconds": 0.0,
    }


def build_comparison_record(
    case: Mapping[str, Any],
    *,
    dry_run: bool = True,
    legacy_config: ComparisonConfig | None = None,
    playbook_config: ComparisonConfig | None = None,
) -> dict[str, Any]:
    if not dry_run:
        raise RuntimeError("live comparison requires explicit legacy and host adapters")
    configured = _config(case)
    legacy_config = legacy_config or configured
    playbook_config = playbook_config or configured
    assert_compatible(legacy_config, playbook_config)

    playbook_path = _resolve_playbook(case)
    playbook = load_playbook(playbook_path)
    plan = Compiler().compile(playbook, dict(case["inputs"]), source_path=str(playbook_path))
    skill_hashes = {
        str(item["id"]): str(item["content_hash"])
        for item in case["skill_library"].get("skills", [])
    }
    record = {
        "schema_version": "agent-playbook-os/comparison/v1",
        "scenario_id": str(case["id"]),
        "assessment": "structural-only",
        "configuration": {
            "skill_library_hash": configured.skill_library_hash,
            "model": redact({
                "provider": configured.provider,
                "name": configured.model,
                "config_hash": configured.model_config_hash,
                "credentials": case.get("provider_credentials"),
            }),
        },
        "inputs": redact(dict(case["inputs"])),
        "paths": {
            "legacy_skill_orchestrator": {
                "status": "not_configured",
                "reason": "live legacy host adapter was not supplied",
                "approvals": 0,
                "retries": 0,
                "usage": {"model_calls": 0, "tool_calls": 0, "wall_time_seconds": 0.0},
                "quality_gates": {"structural": "unverified", "quality": "unverified"},
            },
            "agent_playbook_os": {
                "status": "compiled",
                "playbook_hash": plan.playbook_hash,
                "semantic_hash": plan.semantic_hash,
                "skill_hashes": skill_hashes,
                "approvals": 0,
                "retries": 0,
                "usage": _usage_for_plan(plan),
                "artifacts": [],
                "evidence": [],
                "quality_gates": {"structural": "passed", "quality": "unverified"},
            },
        },
        "interpretation": (
            "Compilation and lock-shape checks passed. Factuality, model quality, "
            "provider behavior and business outcome were not measured."
        ),
    }
    return redact(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("pass --dry-run for the structural fixture; live adapters are not implicit")
    record = build_comparison_record(load_case(args.case), dry_run=True)
    payload = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
