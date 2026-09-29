from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .models import CompiledPlan, StepSpec


class CapabilityRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    required_by: list[str] = Field(default_factory=list)
    mandatory: bool = True
    reason: str


class NegotiationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    requirements: list[CapabilityRequirement] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    available: list[str] = Field(default_factory=list)


def _walk(steps: Iterable[StepSpec]):
    for step in steps:
        yield step
        if step.type == "parallel":
            for branch in step.branches.values():
                yield from _walk(branch)
        elif step.type == "foreach":
            yield from _walk(step.foreach_steps)
        elif step.type == "while":
            yield from _walk(step.loop_steps)


def requirements_for_plan(plan: CompiledPlan) -> list[CapabilityRequirement]:
    by_id: dict[str, CapabilityRequirement] = {}

    def add(capability_id: str, step_id: str, reason: str, mandatory: bool = True):
        existing = by_id.get(capability_id)
        if existing is None:
            by_id[capability_id] = CapabilityRequirement(
                id=capability_id,
                required_by=[step_id],
                mandatory=mandatory,
                reason=reason,
            )
        elif step_id not in existing.required_by:
            existing.required_by.append(step_id)

    for step in _walk([x.spec for x in plan.steps]):
        if step.type == "action":
            add(f"action:{step.action}", step.id, f"action step requires {step.action}")
        elif step.type == "skill":
            add("kind:skill-runtime", step.id, "skill execution requires a skill runtime")
        elif step.type == "agent":
            add("kind:agent-runtime", step.id, "agent execution requires an agent runtime")
        elif step.type == "eval":
            add("kind:eval-runtime", step.id, "eval execution requires an eval runtime")
        isolation = step.execution_isolation or plan.execution.isolation
        required = plan.policy.require_isolation_for.get(step.side_effects)
        effective = required or isolation
        if effective in {"sandbox", "subagent"}:
            add(f"isolation:{effective}", step.id, f"step requires {effective} isolation")
        if step.side_effects in {"external", "destructive"}:
            add("kind:receipt", step.id, "provider receipts improve recovery of material side effects", mandatory=False)
    return sorted(by_id.values(), key=lambda x: x.id)


def negotiate_runtime(plan: CompiledPlan, runtime) -> NegotiationReport:
    fn = getattr(runtime, "capabilities", None)
    available_items = fn().list() if callable(fn) else []
    ids = {x.id for x in available_items}
    kinds = {x.kind for x in available_items}
    requirements = requirements_for_plan(plan)
    missing: list[str] = []
    warnings: list[str] = []
    for req in requirements:
        supported = req.id in ids
        if req.id.startswith("kind:"):
            supported = req.id.split(":", 1)[1] in kinds
        if not supported:
            if req.mandatory:
                missing.append(req.id)
            else:
                warnings.append(f"optional capability missing: {req.id} ({req.reason})")
    return NegotiationReport(
        passed=not missing,
        requirements=requirements,
        missing=sorted(set(missing)),
        warnings=warnings,
        available=sorted(ids),
    )
