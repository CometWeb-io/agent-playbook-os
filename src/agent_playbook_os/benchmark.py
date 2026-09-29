from __future__ import annotations

from pathlib import Path
import statistics
import tempfile
import time
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from .engine import Runner


class BenchmarkSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    status: str
    wall_ms: float
    cost_usd: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0


class BenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repeats: int
    succeeded: int
    failed: int
    median_ms: float
    p95_ms: float
    total_cost_usd: float
    samples: list[BenchmarkSample] = Field(default_factory=list)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


async def benchmark_plan(
    plan,
    runtime_factory: Callable[[], object],
    *,
    repeats: int = 5,
    root: str | Path | None = None,
    approvals: set[str] | None = None,
    approval_actor: str = "benchmark",
    runner_factory: Callable[[object], Runner] | None = None,
):
    if repeats < 1 or repeats > 1000:
        raise ValueError("repeats must be between 1 and 1000")
    samples: list[BenchmarkSample] = []
    base = Path(root) if root else Path(tempfile.mkdtemp(prefix="apbos-bench-"))
    base.mkdir(parents=True, exist_ok=True)
    for i in range(repeats):
        started = time.perf_counter()
        runtime = runtime_factory()
        runner = runner_factory(runtime) if runner_factory else Runner(runtime)
        state = await runner.run(
            plan,
            base / f"run-{i:04d}",
            approvals=set(approvals or set()),
            approval_actor=approval_actor,
        )
        wall = (time.perf_counter() - started) * 1000
        samples.append(BenchmarkSample(
            index=i,
            status=state.status.value,
            wall_ms=wall,
            cost_usd=state.usage.cost_usd,
            model_calls=state.usage.model_calls,
            tool_calls=state.usage.tool_calls,
        ))
    walls = [x.wall_ms for x in samples]
    succeeded = sum(x.status == "COMPLETED" for x in samples)
    return BenchmarkReport(
        repeats=repeats,
        succeeded=succeeded,
        failed=repeats - succeeded,
        median_ms=statistics.median(walls),
        p95_ms=_p95(walls),
        total_cost_usd=sum(x.cost_usd for x in samples),
        samples=samples,
    )
