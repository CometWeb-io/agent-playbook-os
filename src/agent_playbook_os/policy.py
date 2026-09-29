from __future__ import annotations

from .errors import PolicyDenied
from .models import BudgetSpec, CompiledPlan, PolicySpec, StepSpec


def _ceiling_ok(requested, ceiling) -> bool:
    return ceiling is None or requested is None or requested <= ceiling


class PolicyEngine:
    def __init__(self, policy: PolicySpec):
        self.policy = policy

    def check_plan(self, plan: CompiledPlan):
        p = self.policy
        total = self._count_tree([x.spec for x in plan.steps])
        if p.max_steps is not None and total > p.max_steps:
            raise PolicyDenied(f"plan has {total} steps including nested steps; policy max is {p.max_steps}")
        if p.max_parallel is not None and plan.execution.max_parallel > p.max_parallel:
            raise PolicyDenied("execution max_parallel exceeds policy")
        if p.max_foreach_items is not None and plan.execution.max_foreach_items > p.max_foreach_items:
            raise PolicyDenied("execution max_foreach_items exceeds policy")
        if p.max_loop_iterations is not None and plan.execution.max_loop_iterations > p.max_loop_iterations:
            raise PolicyDenied("execution max_loop_iterations exceeds policy")
        self._check_budget_ceiling(plan.execution.budget, p.budget)
        if p.require_resolved_skills:
            unresolved = sorted(sid for sid, lock in plan.skill_lock.items() if not lock.resolved)
            if unresolved:
                raise PolicyDenied(f"policy requires resolved skill locks: {unresolved}")
        for cs in plan.steps:
            self._check_tree(cs.spec)

    def _check_budget_ceiling(self, requested: BudgetSpec, ceiling: BudgetSpec):
        for name in BudgetSpec.model_fields:
            rv = getattr(requested, name)
            cv = getattr(ceiling, name)
            if not _ceiling_ok(rv, cv):
                raise PolicyDenied(f"execution budget {name}={rv} exceeds policy ceiling {cv}")

    def _count_tree(self, steps: list[StepSpec]) -> int:
        count = 0
        for step in steps:
            count += 1
            if step.type == "parallel":
                for branch in step.branches.values():
                    count += self._count_tree(branch)
            elif step.type == "foreach":
                count += self._count_tree(step.foreach_steps)
            elif step.type == "while":
                count += self._count_tree(step.loop_steps)
        return count

    def _check_tree(self, step: StepSpec):
        self.check_step(step)
        if step.type == "parallel":
            for branch in step.branches.values():
                for nested in branch:
                    self._check_tree(nested)
        elif step.type == "foreach":
            for nested in step.foreach_steps:
                self._check_tree(nested)
        elif step.type == "while":
            for nested in step.loop_steps:
                self._check_tree(nested)

    def check_step(self, step: StepSpec):
        p = self.policy
        if p.allowed_step_types is not None and step.type not in p.allowed_step_types:
            raise PolicyDenied(f"step type denied by allowlist: {step.type}")
        if step.type == "skill":
            if step.uses in p.denied_skills:
                raise PolicyDenied(f"skill denied by policy: {step.uses}")
            if p.allowed_skills is not None and step.uses not in p.allowed_skills:
                raise PolicyDenied(f"skill not in policy allowlist: {step.uses}")
        if step.type == "action":
            if step.action in p.denied_actions:
                raise PolicyDenied(f"action denied by policy: {step.action}")
            if p.allowed_actions is not None and step.action not in p.allowed_actions:
                raise PolicyDenied(f"action not in policy allowlist: {step.action}")
        if (
            step.retry.max_attempts > 1
            and step.side_effects in p.require_idempotency_for_retry
            and not step.idempotency_key
        ):
            raise PolicyDenied(
                f"step {step.id} retries {step.side_effects} side effects without an idempotency_key"
            )

        if self.policy.budget.max_step_seconds is not None and step.timeout_seconds is not None and step.timeout_seconds > self.policy.budget.max_step_seconds:
            raise PolicyDenied(f"step {step.id} timeout_seconds exceeds policy max_step_seconds")

    def approval_required(self, step: StepSpec) -> bool:
        return step.requires_approval or step.side_effects in self.policy.require_approval_for


def _intersect_allowlist(a: list[str] | None, b: list[str] | None) -> list[str] | None:
    if a is None:
        return list(b) if b is not None else None
    if b is None:
        return list(a)
    return sorted(set(a) & set(b))


def _min_optional(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _merge_budget_ceiling(a: BudgetSpec, b: BudgetSpec) -> BudgetSpec:
    values = {}
    for name in BudgetSpec.model_fields:
        values[name] = _min_optional(getattr(a, name), getattr(b, name))
    return BudgetSpec(**values)


def compose_policies(playbook_policy: PolicySpec, host_policy: PolicySpec | None) -> PolicySpec:
    """Compose playbook and host policies without allowing a playbook to weaken the host.

    Allowlists intersect, denylists/mandatory approval classes union, numeric ceilings take
    the minimum, and host isolation requirements win on direct conflicts because they
    describe a host-owned trust boundary.
    """
    if host_policy is None:
        return playbook_policy.model_copy(deep=True)
    isolation = dict(playbook_policy.require_isolation_for)
    isolation.update(host_policy.require_isolation_for)
    return PolicySpec(
        allowed_step_types=_intersect_allowlist(playbook_policy.allowed_step_types, host_policy.allowed_step_types),
        allowed_skills=_intersect_allowlist(playbook_policy.allowed_skills, host_policy.allowed_skills),
        allowed_actions=_intersect_allowlist(playbook_policy.allowed_actions, host_policy.allowed_actions),
        denied_skills=sorted(set(playbook_policy.denied_skills) | set(host_policy.denied_skills)),
        denied_actions=sorted(set(playbook_policy.denied_actions) | set(host_policy.denied_actions)),
        require_approval_for=sorted(
            set(playbook_policy.require_approval_for) | set(host_policy.require_approval_for)
        ),
        require_idempotency_for_retry=sorted(
            set(playbook_policy.require_idempotency_for_retry) | set(host_policy.require_idempotency_for_retry)
        ),
        max_steps=_min_optional(playbook_policy.max_steps, host_policy.max_steps),
        max_parallel=_min_optional(playbook_policy.max_parallel, host_policy.max_parallel),
        max_foreach_items=_min_optional(playbook_policy.max_foreach_items, host_policy.max_foreach_items),
        max_loop_iterations=_min_optional(playbook_policy.max_loop_iterations, host_policy.max_loop_iterations),
        require_resolved_skills=playbook_policy.require_resolved_skills or host_policy.require_resolved_skills,
        require_isolation_for=isolation,
        budget=_merge_budget_ceiling(playbook_policy.budget, host_policy.budget),
    )


def assert_plan_satisfies_host_policy(plan: CompiledPlan, host_policy: PolicySpec | None) -> None:
    """Reject a persisted plan when the current host policy would make it stricter.

    Persisted compiled plans are immutable. We therefore cannot silently rewrite their
    effective policy during resume/replay/run-plan; callers must recompile the source
    under the current host policy so the new policy is part of the plan identity.
    """
    if host_policy is None:
        return
    recomposed = compose_policies(plan.policy, host_policy)
    def normalized(policy: PolicySpec):
        data = policy.model_dump(mode="json")
        for key in (
            "allowed_step_types", "allowed_skills", "allowed_actions", "denied_skills", "denied_actions",
            "require_approval_for", "require_idempotency_for_retry",
        ):
            value = data.get(key)
            if value is not None:
                data[key] = sorted(set(value))
        return data

    if normalized(recomposed) != normalized(plan.policy):
        raise PolicyDenied(
            "compiled plan does not satisfy the current host policy; recompile the source under the current policy"
        )
