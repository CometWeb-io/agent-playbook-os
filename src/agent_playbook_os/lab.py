from __future__ import annotations

from pathlib import Path
import statistics
import tempfile
import time
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from .engine import Runner
from .integrity import canonical_hash


class LabSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: str
    repeat: int
    run_id: str
    status: str
    output_hash: str
    wall_ms: float
    cost_usd: float
    model_calls: int
    tool_calls: int


class VariantReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    repeats: int
    completed: int
    failed: int
    output_hashes: list[str] = Field(default_factory=list)
    output_consistency: float
    median_ms: float
    p95_ms: float
    total_cost_usd: float
    samples: list[LabSample] = Field(default_factory=list)


class VariantDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: str
    candidate: str
    status_match: bool
    output_hash_match: bool
    median_ms_delta: float
    total_cost_delta_usd: float


class ReplayLabReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: str
    variants: list[VariantReport]
    deltas: list[VariantDelta]


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


async def compare_runtimes(
    plan,
    variants: dict[str, Callable[[], object]],
    *,
    repeats: int = 1,
    root: str | Path | None = None,
    approvals: set[str] | None = None,
    runner_factory: Callable[[object], Runner] | None = None,
) -> ReplayLabReport:
    if not variants:
        raise ValueError("at least one variant is required")
    if repeats < 1 or repeats > 100:
        raise ValueError("repeats must be between 1 and 100")
    base = Path(root) if root else Path(tempfile.mkdtemp(prefix="apbos-lab-"))
    base.mkdir(parents=True, exist_ok=True)
    reports: list[VariantReport] = []

    for name, factory in variants.items():
        samples: list[LabSample] = []
        for repeat in range(repeats):
            runtime = factory()
            runner = runner_factory(runtime) if runner_factory else Runner(runtime)
            run_dir = base / name / f"run-{repeat:04d}"
            started = time.perf_counter()
            state = await runner.run(plan, run_dir, approvals=set(approvals or set()))
            wall_ms = (time.perf_counter() - started) * 1000
            samples.append(LabSample(
                variant=name,
                repeat=repeat,
                run_id=state.run_id,
                status=state.status.value,
                output_hash=canonical_hash(state.outputs),
                wall_ms=wall_ms,
                cost_usd=state.usage.cost_usd,
                model_calls=state.usage.model_calls,
                tool_calls=state.usage.tool_calls,
            ))
        hashes = [x.output_hash for x in samples]
        modal_count = max((hashes.count(x) for x in set(hashes)), default=0)
        walls = [x.wall_ms for x in samples]
        reports.append(VariantReport(
            name=name,
            repeats=repeats,
            completed=sum(x.status == "COMPLETED" for x in samples),
            failed=sum(x.status != "COMPLETED" for x in samples),
            output_hashes=sorted(set(hashes)),
            output_consistency=(modal_count / repeats) if repeats else 0.0,
            median_ms=statistics.median(walls) if walls else 0.0,
            p95_ms=_p95(walls),
            total_cost_usd=sum(x.cost_usd for x in samples),
            samples=samples,
        ))

    baseline = reports[0]
    deltas = []
    baseline_status = baseline.completed == baseline.repeats
    baseline_hash = baseline.output_hashes[0] if len(baseline.output_hashes) == 1 else None
    for candidate in reports[1:]:
        candidate_status = candidate.completed == candidate.repeats
        candidate_hash = candidate.output_hashes[0] if len(candidate.output_hashes) == 1 else None
        deltas.append(VariantDelta(
            baseline=baseline.name,
            candidate=candidate.name,
            status_match=baseline_status == candidate_status,
            output_hash_match=baseline_hash is not None and baseline_hash == candidate_hash,
            median_ms_delta=candidate.median_ms - baseline.median_ms,
            total_cost_delta_usd=candidate.total_cost_usd - baseline.total_cost_usd,
        ))
    return ReplayLabReport(baseline=baseline.name, variants=reports, deltas=deltas)
