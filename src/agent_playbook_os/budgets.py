from __future__ import annotations

from datetime import datetime, timezone

from .errors import BudgetExceeded
from .models import BudgetSpec, CompiledPlan, RunState


def _min_limit(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


class BudgetController:
    def __init__(self, plan: CompiledPlan):
        self.plan = plan
        self.execution = plan.execution.budget
        self.policy = plan.policy.budget

    def limit(self, field: str):
        return _min_limit(getattr(self.execution, field), getattr(self.policy, field))

    def elapsed_seconds(self, state: RunState) -> float:
        if not state.started_at:
            return 0.0
        started = datetime.fromisoformat(state.started_at)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())

    def remaining_run_seconds(self, state: RunState) -> float | None:
        limit = self.limit("max_run_seconds")
        if limit is None:
            return None
        return max(0.0, limit - self.elapsed_seconds(state))

    def effective_step_timeout(self, step_timeout: float | None, state: RunState) -> float | None:
        timeout = step_timeout
        timeout = _min_limit(timeout, self.limit("max_step_seconds"))
        timeout = _min_limit(timeout, self.remaining_run_seconds(state))
        return timeout

    def check_can_invoke(self, state: RunState, step_type: str):
        checks = [("max_total_attempts", state.usage.total_attempts + 1)]
        field = {
            "action": ("max_action_calls", state.usage.action_calls + 1),
            "skill": ("max_skill_calls", state.usage.skill_calls + 1),
            "agent": ("max_agent_calls", state.usage.agent_calls + 1),
            "eval": ("max_eval_calls", state.usage.eval_calls + 1),
        }.get(step_type)
        if field:
            checks.append(field)
        for budget_field, projected in checks:
            limit = self.limit(budget_field)
            if limit is not None and projected > limit:
                raise BudgetExceeded(
                    f"budget would be exceeded before invocation: {budget_field} projected={projected} limit={limit}"
                )

    def check(self, state: RunState):
        state.usage.wall_time_seconds = self.elapsed_seconds(state)
        checks = {
            "max_total_attempts": state.usage.total_attempts,
            "max_action_calls": state.usage.action_calls,
            "max_skill_calls": state.usage.skill_calls,
            "max_agent_calls": state.usage.agent_calls,
            "max_eval_calls": state.usage.eval_calls,
            "max_model_calls": state.usage.model_calls,
            "max_tool_calls": state.usage.tool_calls,
            "max_input_tokens": state.usage.input_tokens,
            "max_output_tokens": state.usage.output_tokens,
            "max_cost_usd": state.usage.cost_usd,
        }
        run_limit = self.limit("max_run_seconds")
        if run_limit is not None and state.usage.wall_time_seconds >= run_limit:
            raise BudgetExceeded(f"run time budget exceeded: {state.usage.wall_time_seconds:.3f}s >= {run_limit}s")
        for field, used in checks.items():
            limit = self.limit(field)
            if limit is not None and used > limit:
                raise BudgetExceeded(f"budget exceeded: {field} used={used} limit={limit}")
