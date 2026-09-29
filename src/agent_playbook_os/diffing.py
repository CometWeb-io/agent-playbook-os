from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .storage import open_run_store


class StepDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    status_before: str | None = None
    status_after: str | None = None
    attempts_before: int = 0
    attempts_after: int = 0
    output_changed: bool = False
    error_before: str | None = None
    error_after: str | None = None


class RunDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before: str
    after: str
    playbook_same: bool
    plan_semantics_same: bool
    status_before: str
    status_after: str
    outputs_changed: bool
    usage_delta: dict[str, float | int] = Field(default_factory=dict)
    step_diffs: list[StepDiff] = Field(default_factory=list)


def diff_runs(before: str | Path, after: str | Path) -> RunDiff:
    a = open_run_store(before).load_state()
    b = open_run_store(after).load_state()
    usage_delta: dict[str, float | int] = {}
    for field in type(a.usage).model_fields:
        usage_delta[field] = getattr(b.usage, field) - getattr(a.usage, field)
    step_diffs = []
    for sid in sorted(set(a.steps) | set(b.steps)):
        x = a.steps.get(sid)
        y = b.steps.get(sid)
        if x is None or y is None or (
            x.status != y.status
            or x.attempts != y.attempts
            or x.output != y.output
            or x.error != y.error
        ):
            step_diffs.append(StepDiff(
                step_id=sid,
                status_before=x.status.value if x else None,
                status_after=y.status.value if y else None,
                attempts_before=x.attempts if x else 0,
                attempts_after=y.attempts if y else 0,
                output_changed=(x.output if x else None) != (y.output if y else None),
                error_before=x.error if x else None,
                error_after=y.error if y else None,
            ))
    return RunDiff(
        before=str(Path(before)),
        after=str(Path(after)),
        playbook_same=(a.playbook_hash == b.playbook_hash),
        plan_semantics_same=(a.plan_semantic_hash == b.plan_semantic_hash),
        status_before=a.status.value,
        status_after=b.status.value,
        outputs_changed=a.outputs != b.outputs,
        usage_delta=usage_delta,
        step_diffs=step_diffs,
    )
