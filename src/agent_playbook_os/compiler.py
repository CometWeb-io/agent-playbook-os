from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from .errors import CompileError
from .integrity import canonical_hash, compiled_plan_integrity, compiled_plan_semantic_hash
from .models import CompiledPlan, CompiledStep, Playbook, SkillLockEntry, StepSpec
from .policy import PolicyEngine, compose_policies
from .skills import SkillResolver

_STEP_REF = re.compile(r"\bsteps\.([A-Za-z0-9_-]+)\b")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


class Compiler:
    def __init__(
        self, skill_resolver: SkillResolver | None = None, strict_skill_resolution: bool = False,
        host_policy=None,
    ):
        self.skill_resolver = skill_resolver
        self.strict_skill_resolution = strict_skill_resolution
        self.host_policy = host_policy

    def compile(
        self,
        playbook: Playbook,
        inputs: dict[str, Any] | None = None,
        source_path: str | None = None,
    ) -> CompiledPlan:
        inputs = dict(inputs or {})
        self._validate_inputs(playbook, inputs)
        values = self._materialize_inputs(playbook, inputs)
        steps = playbook.spec.steps

        all_steps = list(self._walk_steps(steps))
        if len(all_steps) > playbook.spec.execution.max_steps:
            raise CompileError(
                f"playbook has {len(all_steps)} steps including nested branches; exceeds execution.max_steps"
            )
        ids = [s.id for s in all_steps]
        if len(ids) != len(set(ids)):
            duplicates = sorted({x for x in ids if ids.count(x) > 1})
            raise CompileError(f"duplicate step id across playbook tree: {duplicates}")

        top_ids = {s.id for s in steps}
        for s in steps:
            missing = set(s.needs) - top_ids
            if missing:
                raise CompileError(f"step {s.id} needs unknown top-level steps: {sorted(missing)}")
            self._validate_refs(s, allowed_needs=set(s.needs))
            self._validate_retry(s, playbook)
            self._validate_output_schema_contract(s)
            if s.type == "parallel":
                self._validate_parallel(s, top_ids, playbook)
            elif s.type == "foreach":
                self._validate_foreach(s, top_ids, playbook)
            elif s.type == "while":
                self._validate_loop(s, top_ids, playbook)

        ordered = self._topological_order(steps)
        skill_lock = self._resolve_skills(playbook, ordered)
        effective_policy = compose_policies(playbook.spec.policy, self.host_policy)
        plan = CompiledPlan(
            playbook_id=playbook.metadata.id,
            playbook_version=playbook.metadata.version,
            playbook_hash=canonical_hash(playbook.model_dump(by_alias=True, mode="json")),
            semantic_hash="pending",
            integrity_hash="pending",
            compiled_at=datetime.now(timezone.utc).isoformat(),
            input_values=values,
            execution=playbook.spec.execution,
            policy=effective_policy,
            playbook_policy_hash=canonical_hash(playbook.spec.policy.model_dump(mode="json")),
            host_policy_hash=canonical_hash(self.host_policy.model_dump(mode="json")) if self.host_policy else None,
            effective_policy_hash=canonical_hash(effective_policy.model_dump(mode="json")),
            steps=[CompiledStep(spec=s, ordinal=i) for i, s in enumerate(ordered)],
            outputs=playbook.spec.outputs,
            skill_lock=skill_lock,
            source_path=str(Path(source_path).resolve()) if source_path else None,
        )
        PolicyEngine(plan.policy).check_plan(plan)
        plan.semantic_hash = compiled_plan_semantic_hash(plan)
        plan.integrity_hash = compiled_plan_integrity(plan)
        return plan

    def _walk_steps(self, steps: list[StepSpec]):
        for step in steps:
            yield step
            if step.type == "parallel":
                for branch in step.branches.values():
                    yield from self._walk_steps(branch)
            elif step.type == "foreach":
                yield from self._walk_steps(step.foreach_steps)
            elif step.type == "while":
                yield from self._walk_steps(step.loop_steps)

    def _validate_refs(self, step: StepSpec, allowed_needs: set[str]):
        # Nested branches have their own dependency scope and are validated separately.
        raw = step.model_dump(by_alias=True, exclude={"branches", "foreach_steps", "loop_steps"})
        refs = set()
        for text in _walk_strings(raw):
            refs.update(_STEP_REF.findall(text))
        undeclared = refs - allowed_needs - {step.id}
        if undeclared:
            raise CompileError(
                f"step {step.id} references {sorted(undeclared)} but does not declare them in needs"
            )

    def _validate_parallel(self, parent: StepSpec, top_ids: set[str], playbook: Playbook):
        branch_ids = {name: {x.id for x in steps} for name, steps in parent.branches.items()}
        all_nested = set().union(*branch_ids.values()) if branch_ids else set()
        for name, branch in parent.branches.items():
            local_ids = branch_ids[name]
            other_branch_ids = all_nested - local_ids
            for nested in branch:
                cross = set(nested.needs) & other_branch_ids
                if cross:
                    raise CompileError(
                        f"parallel branch {name} step {nested.id} depends on another branch: {sorted(cross)}"
                    )
                unknown = set(nested.needs) - local_ids - top_ids
                if unknown:
                    raise CompileError(
                        f"parallel branch {name} step {nested.id} needs unknown steps: {sorted(unknown)}"
                    )
                external = set(nested.needs) & top_ids
                if external - set(parent.needs):
                    raise CompileError(
                        f"parallel step {parent.id} must declare external nested dependencies in its needs: "
                        f"{sorted(external - set(parent.needs))}"
                    )
                self._validate_refs(nested, allowed_needs=set(nested.needs))
                self._validate_retry(nested, playbook)
                self._validate_output_schema_contract(nested)
                if nested.type == "parallel":
                    raise CompileError("nested parallel inside parallel is not supported by playbook.agent/v1alpha1")
            self._topological_order_with_external(branch, set(parent.needs))

    def _validate_foreach(self, parent: StepSpec, top_ids: set[str], playbook: Playbook):
        local_ids = {x.id for x in parent.foreach_steps}
        for nested in parent.foreach_steps:
            unknown = set(nested.needs) - local_ids - top_ids
            if unknown:
                raise CompileError(
                    f"foreach step {parent.id} nested step {nested.id} needs unknown steps: {sorted(unknown)}"
                )
            external = set(nested.needs) & top_ids
            if external - set(parent.needs):
                raise CompileError(
                    f"foreach step {parent.id} must declare external nested dependencies in its needs: "
                    f"{sorted(external - set(parent.needs))}"
                )
            self._validate_refs(nested, allowed_needs=set(nested.needs))
            self._validate_retry(nested, playbook)
            self._validate_output_schema_contract(nested)
            if nested.type in {"parallel", "foreach", "while"}:
                raise CompileError(
                    f"nested {nested.type} inside foreach is not supported by playbook.agent/v1alpha1"
                )
        self._topological_order_with_external(parent.foreach_steps, set(parent.needs))

    def _validate_loop(self, parent: StepSpec, top_ids: set[str], playbook: Playbook):
        if parent.max_iterations is None:
            raise CompileError(f"while step {parent.id} requires max_iterations")
        if parent.max_iterations > playbook.spec.execution.max_loop_iterations:
            raise CompileError(
                f"while step {parent.id} max_iterations={parent.max_iterations} exceeds "
                f"execution.max_loop_iterations={playbook.spec.execution.max_loop_iterations}"
            )
        local_ids = {x.id for x in parent.loop_steps}
        for nested in parent.loop_steps:
            unknown = set(nested.needs) - local_ids - top_ids
            if unknown:
                raise CompileError(
                    f"while step {parent.id} nested step {nested.id} needs unknown steps: {sorted(unknown)}"
                )
            external = set(nested.needs) & top_ids
            if external - set(parent.needs):
                raise CompileError(
                    f"while step {parent.id} must declare external nested dependencies in its needs: "
                    f"{sorted(external - set(parent.needs))}"
                )
            self._validate_refs(nested, allowed_needs=set(nested.needs))
            self._validate_retry(nested, playbook)
            self._validate_output_schema_contract(nested)
            if nested.type in {"parallel", "foreach", "while"}:
                raise CompileError(
                    f"nested {nested.type} inside while is not supported by playbook.agent/v1alpha1"
                )
        self._topological_order_with_external(parent.loop_steps, set(parent.needs))

    def _validate_output_schema_contract(self, step: StepSpec):
        if step.output_schema and step.output_schema.startswith(("http://", "https://")) and not step.output_schema_hash:
            raise CompileError(
                f"remote output_schema for step {step.id} requires output_schema_hash pin"
            )

    def _validate_retry(self, step: StepSpec, playbook: Playbook):
        if step.retry.max_attempts > playbook.spec.execution.max_attempts:
            raise CompileError(
                f"step {step.id} retry.max_attempts={step.retry.max_attempts} exceeds execution.max_attempts="
                f"{playbook.spec.execution.max_attempts}"
            )
        if (
            step.retry.max_attempts > 1
            and step.side_effects in playbook.spec.policy.require_idempotency_for_retry
            and not step.idempotency_key
        ):
            raise CompileError(
                f"step {step.id} retries {step.side_effects} side effects but has no idempotency_key"
            )

    def _validate_inputs(self, playbook: Playbook, provided: dict[str, Any]):
        defined = playbook.spec.inputs
        unknown = set(provided) - set(defined)
        if unknown:
            raise CompileError(f"unknown inputs: {sorted(unknown)}")
        missing = [k for k, v in defined.items() if v.required and k not in provided and v.default is None]
        if missing:
            raise CompileError(f"missing required inputs: {missing}")
        for name, definition in defined.items():
            value = provided.get(name, definition.default)
            if definition.sensitive and value is not None:
                if not isinstance(value, str) or not value.startswith(("env://", "secret://", "file-secret://")):
                    raise CompileError(
                        f"sensitive input {name} must be a secret reference (env://, secret://, file-secret://), not a raw value"
                    )

    def _materialize_inputs(self, playbook: Playbook, provided: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for name, definition in playbook.spec.inputs.items():
            value = provided[name] if name in provided else definition.default
            if value is not None:
                self._check_type(name, value, definition.type)
            result[name] = value
        return result

    def _check_type(self, name, value, expected):
        mapping = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        if expected == "integer" and isinstance(value, bool):
            raise CompileError(f"input {name} expected integer")
        if expected == "number" and isinstance(value, bool):
            raise CompileError(f"input {name} expected number")
        if not isinstance(value, mapping[expected]):
            raise CompileError(f"input {name} expected {expected}, got {type(value).__name__}")

    def _topological_order(self, steps: list[StepSpec]) -> list[StepSpec]:
        return self._topological_order_with_external(steps, set())

    def _topological_order_with_external(self, steps: list[StepSpec], external: set[str]) -> list[StepSpec]:
        by_id = {s.id: s for s in steps}
        internal = set(by_id)
        indegree = {s.id: len([d for d in s.needs if d in internal]) for s in steps}
        dependents = {s.id: [] for s in steps}
        for s in steps:
            for dep in s.needs:
                if dep in internal:
                    dependents[dep].append(s.id)
                elif dep not in external:
                    raise CompileError(f"step {s.id} needs unknown step {dep}")
        queue = [s.id for s in steps if indegree[s.id] == 0]
        result: list[StepSpec] = []
        while queue:
            sid = queue.pop(0)
            result.append(by_id[sid])
            for child in dependents[sid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(result) != len(steps):
            cycle = [k for k, v in indegree.items() if v > 0]
            raise CompileError(f"dependency cycle detected: {cycle}")
        return result

    def _collect_skills(self, steps: list[StepSpec]):
        used = set()
        for step in steps:
            if step.type == "skill" and step.uses:
                used.add(step.uses)
            if step.type == "parallel":
                for branch in step.branches.values():
                    used.update(self._collect_skills(branch))
            elif step.type == "foreach":
                used.update(self._collect_skills(step.foreach_steps))
            elif step.type == "while":
                used.update(self._collect_skills(step.loop_steps))
        return used

    def _resolve_skills(self, playbook: Playbook, ordered: list[StepSpec]):
        used = self._collect_skills(ordered)
        declared = playbook.spec.dependencies.skills
        missing_decl = used - set(declared)
        if missing_decl:
            raise CompileError(f"skill dependencies not declared: {sorted(missing_decl)}")
        locks: dict[str, SkillLockEntry] = {}
        for sid in sorted(used):
            dep = declared[sid]
            if self.skill_resolver:
                lock = self.skill_resolver.lock(sid, dep)
                if self.strict_skill_resolution and not lock.resolved and not dep.optional:
                    raise CompileError(f"required skill could not be resolved: {sid}")
                locks[sid] = lock
            else:
                locks[sid] = SkillLockEntry(
                    id=sid,
                    source=dep.source,
                    version=dep.version,
                    ref=dep.ref,
                    path=dep.path,
                    content_hash=None,
                    resolved=False,
                )
        return locks
