from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .runtime import ExecutionRuntime, ReferenceRuntime


@dataclass(frozen=True)
class FaultRule:
    step_id: str
    attempts: frozenset[int] = field(default_factory=lambda: frozenset({1}))
    message: str = "injected failure"


class FaultInjectingRuntime:
    """Runtime wrapper for deterministic failure-injection and recovery tests."""

    def __init__(self, inner: ExecutionRuntime | None = None, rules: list[FaultRule] | None = None):
        self.inner = inner or ReferenceRuntime(dry_run=True)
        self.rules = {r.step_id: r for r in (rules or [])}
        self.counts: dict[str, int] = defaultdict(int)

    def _before(self, step):
        self.counts[step.id] += 1
        rule = self.rules.get(step.id)
        if rule and self.counts[step.id] in rule.attempts:
            raise RuntimeError(rule.message)

    async def execute_skill(self, step, inputs, context):
        self._before(step)
        return await self.inner.execute_skill(step, inputs, context)

    async def execute_action(self, step, inputs, context):
        self._before(step)
        return await self.inner.execute_action(step, inputs, context)

    async def execute_agent(self, step, inputs, context):
        self._before(step)
        return await self.inner.execute_agent(step, inputs, context)

    async def execute_eval(self, step, inputs, context):
        self._before(step)
        return await self.inner.execute_eval(step, inputs, context)
