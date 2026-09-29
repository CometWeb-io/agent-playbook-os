import asyncio
from pathlib import Path

import pytest

from agent_playbook_os.catalog import PlaybookCatalog
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.lab import compare_runtimes
from agent_playbook_os.loader import load_playbook
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.planning import (
    ModelBackedOperator,
    StaticPlannerRuntime,
    prepare_operator_execution,
    run_operator_goal,
)
from agent_playbook_os.promotion import PromotionRegistry
from agent_playbook_os.recording import ReplayRuntime
from agent_playbook_os.runtime import MockRuntime, ReferenceRuntime
from agent_playbook_os.store import RunStore
from agent_playbook_os.errors import CompileError


def pb(data):
    return Playbook.model_validate(data)


def simple_playbook(pid="hello"):
    return pb({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": pid, "version": "1.0.0", "description": "Say hello", "tags": ["demo"]},
        "spec": {
            "inputs": {"name": {"type": "string", "required": True}},
            "steps": [
                {"id": "greet", "type": "action", "action": "set", "with": {"value": "{{ inputs.name }}"}}
            ],
            "outputs": {"greeting": "{{ steps.greet.output }}"},
        },
    })


def test_model_operator_selects_existing(tmp_path):
    import yaml
    playbook = simple_playbook()
    path = tmp_path / "hello.yaml"
    path.write_text(yaml.safe_dump(playbook.model_dump(mode="json", by_alias=True), sort_keys=False))
    catalog = PlaybookCatalog.from_roots([tmp_path])
    operator = ModelBackedOperator(StaticPlannerRuntime({
        "mode": "existing", "selected_id": "hello", "selected_version": "1.0.0",
        "confidence": "high", "rationale": "exact fit",
    }))
    decision = asyncio.run(operator.decide("say hello", catalog))
    assert decision.mode == "existing"
    assert decision.selected.id == "hello"


def test_model_operator_rejects_unknown_selection():
    operator = ModelBackedOperator(StaticPlannerRuntime({
        "mode": "existing", "selected_id": "missing", "confidence": "high", "rationale": "bad",
    }))
    with pytest.raises(CompileError):
        asyncio.run(operator.decide("x", PlaybookCatalog()))


def test_ephemeral_operator_prepare_and_run_records_candidate(tmp_path):
    playbook = simple_playbook("ephemeral-hello")
    response = {
        "mode": "ephemeral",
        "confidence": "medium",
        "rationale": "no catalog match",
        "playbook": playbook.model_dump(mode="json", by_alias=True),
    }
    operator = ModelBackedOperator(StaticPlannerRuntime(response))
    registry = PromotionRegistry(tmp_path / "promotion.jsonl")
    prepared, state = asyncio.run(run_operator_goal(
        goal="hello Ada",
        operator=operator,
        catalog=PlaybookCatalog(),
        compiler=Compiler(),
        runtime=ReferenceRuntime(),
        run_dir=tmp_path / "run",
        inputs={"name": "Ada"},
        promotion_registry=registry,
        approve_ephemeral_plan=True,
    ))
    assert state.status == RunStatus.COMPLETED
    assert prepared.decision.mode == "ephemeral"
    assert "ephemeral" in prepared.playbook.metadata.tags
    assert (tmp_path / "run" / "operator" / "ephemeral-playbook.yaml").exists()
    summary = registry.summary(prepared.playbook, min_successes=1)
    assert summary.ready is True
    assert summary.successes == 1


def test_promotion_requires_evidence_unless_forced(tmp_path):
    playbook = simple_playbook("ephemeral-promote")
    playbook.metadata.tags += ["ephemeral", "operator-generated"]
    registry = PromotionRegistry(tmp_path / "promotion.jsonl")
    with pytest.raises(ValueError):
        registry.promote(playbook, tmp_path / "promoted.yaml", actor="test", min_successes=1)
    receipt = registry.promote(playbook, tmp_path / "promoted.yaml", actor="test", min_successes=1, force=True)
    assert Path(receipt.destination).exists()
    promoted = load_playbook(receipt.destination)
    assert "ephemeral" not in promoted.metadata.tags


def foreach_playbook(require_approval=False):
    inner = {
        "id": "emit",
        "type": "action",
        "action": "set",
        "with": {"value": "{{ item }}"},
    }
    if require_approval:
        inner["requires_approval"] = True
    return pb({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "foreach-demo", "version": "1.0.0", "description": "foreach demo"},
        "spec": {
            "inputs": {"items": {"type": "array", "required": True}},
            "steps": [{
                "id": "fanout",
                "type": "foreach",
                "items": "{{ inputs.items }}",
                "item_var": "item",
                "max_concurrency": 2,
                "steps": [inner],
            }],
            "outputs": {"results": "{{ steps.fanout.output }}"},
        },
    })


def test_foreach_executes_items_in_stable_output_order(tmp_path):
    plan = Compiler().compile(foreach_playbook(), {"items": ["a", "b", "c"]})
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    assert state.status == RunStatus.COMPLETED
    results = state.steps["fanout"].output
    assert [x["item"] for x in results] == ["a", "b", "c"]
    assert [x["outputs"]["emit"] for x in results] == ["a", "b", "c"]
    assert len(state.nested_steps) == 3


def test_foreach_approval_is_durable_across_resume(tmp_path):
    plan = Compiler().compile(foreach_playbook(require_approval=True), {"items": ["a", "b"]})
    run_dir = tmp_path / "run"
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir))
    assert state.status == RunStatus.WAITING_APPROVAL
    pending = sorted(state.pending_approvals)
    assert pending
    state2 = asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir, resume=True, approvals=set(pending)))
    # There may be another independently pending item after first resume.
    if state2.status == RunStatus.WAITING_APPROVAL:
        state2 = asyncio.run(Runner(ReferenceRuntime()).run(
            plan, run_dir, resume=True, approvals=set(state2.pending_approvals)
        ))
    assert state2.status == RunStatus.COMPLETED
    assert len(state2.approvals) == 2


def test_foreach_rejects_nested_foreach():
    data = foreach_playbook().model_dump(mode="json", by_alias=True)
    data["spec"]["steps"][0]["steps"] = [{
        "id": "nested", "type": "foreach", "items": [1],
        "steps": [{"id": "x", "type": "action", "action": "set", "with": {"value": 1}}],
    }]
    with pytest.raises(CompileError, match="nested foreach inside foreach"):
        Compiler().compile(Playbook.model_validate(data), {"items": [1]})


def test_foreach_requires_array_at_runtime(tmp_path):
    data = foreach_playbook().model_dump(mode="json", by_alias=True)
    data["spec"]["steps"][0]["items"] = "not-array"
    plan = Compiler().compile(Playbook.model_validate(data), {"items": [1]})
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    assert state.status == RunStatus.FAILED
    assert "items must resolve to an array" in state.steps["fanout"].error


def test_run_lineage_fields_are_persisted(tmp_path):
    plan = Compiler().compile(simple_playbook(), {"name": "Ada"})
    parent = asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "parent"))
    child = asyncio.run(Runner(ReferenceRuntime()).run(
        plan,
        tmp_path / "child",
        parent_run_id=parent.run_id,
        root_run_id=parent.run_id,
        fork_reason="variant-test",
        parent_lineage_depth=parent.lineage_depth,
    ))
    assert child.parent_run_id == parent.run_id
    assert child.root_run_id == parent.run_id
    assert child.lineage_depth == 1
    assert child.fork_reason == "variant-test"


def test_lab_detects_output_divergence(tmp_path):
    playbook = simple_playbook()
    plan = Compiler().compile(playbook, {"name": "ignored"})
    variants = {
        "a": lambda: MockRuntime(action_outputs={"greet": "A"}),
        "b": lambda: MockRuntime(action_outputs={"greet": "B"}),
    }
    report = asyncio.run(compare_runtimes(plan, variants, repeats=2, root=tmp_path / "lab"))
    assert len(report.variants) == 2
    assert report.variants[0].output_consistency == 1.0
    assert report.deltas[0].output_hash_match is False


def test_prepare_operator_fails_closed_on_missing_runtime_capability(tmp_path):
    playbook = pb({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "custom-action", "version": "1.0.0", "description": "custom action"},
        "spec": {"steps": [{"id": "x", "type": "action", "action": "missing-action"}]},
    })
    response = {
        "mode": "ephemeral", "confidence": "high", "rationale": "test",
        "playbook": playbook.model_dump(mode="json", by_alias=True),
    }
    operator = ModelBackedOperator(StaticPlannerRuntime(response))
    with pytest.raises(CompileError, match="capability preflight"):
        asyncio.run(prepare_operator_execution(
            goal="x", operator=operator, catalog=PlaybookCatalog(), compiler=Compiler(),
            runtime=ReferenceRuntime(), ephemeral_path=tmp_path / "e.yaml", require_capabilities=True,
        ))


def test_operator_decision_has_auditable_hashes():
    operator = ModelBackedOperator(StaticPlannerRuntime({
        "mode": "none", "confidence": "none", "rationale": "no fit",
    }))
    decision = asyncio.run(operator.decide("nothing", PlaybookCatalog()))
    assert decision.request_hash.startswith("sha256:")
    assert decision.response_hash.startswith("sha256:")


def test_ephemeral_identity_collision_is_rejected(tmp_path):
    import yaml
    existing = simple_playbook("collision")
    path = tmp_path / "existing.yaml"
    path.write_text(yaml.safe_dump(existing.model_dump(mode="json", by_alias=True), sort_keys=False))
    catalog = PlaybookCatalog.from_roots([tmp_path])
    operator = ModelBackedOperator(StaticPlannerRuntime({
        "mode": "ephemeral", "confidence": "medium", "rationale": "bad duplicate",
        "playbook": existing.model_dump(mode="json", by_alias=True),
    }))
    with pytest.raises(CompileError, match="collides with catalog identity"):
        asyncio.run(operator.decide("x", catalog))


def test_promotion_requires_distinct_goals_when_configured(tmp_path):
    playbook = simple_playbook("ephemeral-diversity")
    playbook.metadata.tags += ["ephemeral"]
    registry = PromotionRegistry(tmp_path / "promotion.jsonl")
    plan = Compiler().compile(playbook, {"name": "Ada"})
    for idx in range(2):
        run_dir = tmp_path / f"run-{idx}"
        asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir))
        registry.record(playbook, run_dir, goal="same-goal")
    summary = registry.summary(playbook, min_successes=2, min_distinct_goals=2)
    assert summary.successes == 2
    assert summary.distinct_goals == 1
    assert summary.ready is False


def test_host_policy_overlay_cannot_be_weakened_by_playbook():
    from agent_playbook_os.models import PolicySpec
    playbook = simple_playbook()
    playbook.spec.policy.allowed_actions = ["set", "echo"]
    host = PolicySpec(allowed_actions=["echo"])
    with pytest.raises(Exception, match="action not in policy allowlist: set"):
        Compiler(host_policy=host).compile(playbook, {"name": "Ada"})


def test_compiled_plan_records_policy_provenance_hashes():
    from agent_playbook_os.models import PolicySpec
    host = PolicySpec(max_parallel=4)
    plan = Compiler(host_policy=host).compile(simple_playbook(), {"name": "Ada"})
    assert plan.playbook_policy_hash.startswith("sha256:")
    assert plan.host_policy_hash.startswith("sha256:")
    assert plan.effective_policy_hash.startswith("sha256:")


def test_foreach_max_items_is_enforced(tmp_path):
    data = foreach_playbook().model_dump(mode="json", by_alias=True)
    data["spec"]["execution"]["max_foreach_items"] = 2
    plan = Compiler().compile(Playbook.model_validate(data), {"items": [1, 2, 3]})
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    assert state.status == RunStatus.FAILED
    assert "max_foreach_items=2" in state.steps["fanout"].error


def test_promotion_registry_hash_chain_detects_tampering(tmp_path):
    playbook = simple_playbook("ephemeral-chain")
    registry = PromotionRegistry(tmp_path / "promotion.jsonl")
    plan = Compiler().compile(playbook, {"name": "Ada"})
    run_dir = tmp_path / "run"
    asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir))
    registry.record(playbook, run_dir, goal="goal-a")
    assert registry.verify() == (True, None)
    text = registry.path.read_text(encoding="utf-8").replace('"goal":"goal-a"', '"goal":"goal-b"')
    registry.path.write_text(text, encoding="utf-8")
    ok, error = registry.verify()
    assert ok is False
    assert "record_hash mismatch" in error


def test_promotion_can_assign_stable_identity(tmp_path):
    playbook = simple_playbook("ephemeral-stable")
    playbook.metadata.version = "0.0.0-ephemeral"
    registry = PromotionRegistry(tmp_path / "promotion.jsonl")
    receipt = registry.promote(
        playbook, tmp_path / "promoted.yaml", actor="test", force=True,
        new_id="stable-playbook", new_version="1.0.0",
    )
    promoted = load_playbook(tmp_path / "promoted.yaml")
    assert promoted.metadata.id == "stable-playbook"
    assert promoted.metadata.version == "1.0.0"
    assert receipt.promoted_playbook_id == "stable-playbook"


def test_ephemeral_plan_requires_explicit_approval_and_persists_review_artifacts(tmp_path):
    from agent_playbook_os.errors import PlanApprovalRequired

    playbook = simple_playbook("approval-required")
    operator = ModelBackedOperator(StaticPlannerRuntime({
        "mode": "ephemeral",
        "confidence": "high",
        "rationale": "no catalog match",
        "playbook": playbook.model_dump(mode="json", by_alias=True),
    }))
    run_dir = tmp_path / "pending"
    with pytest.raises(PlanApprovalRequired) as exc:
        asyncio.run(run_operator_goal(
            goal="say hello",
            operator=operator,
            catalog=PlaybookCatalog(),
            compiler=Compiler(),
            runtime=ReferenceRuntime(),
            run_dir=run_dir,
            inputs={"name": "Ada"},
        ))
    assert exc.value.plan_semantic_hash.startswith("sha256:")
    assert (run_dir / "operator" / "ephemeral-playbook.yaml").exists()
    assert (run_dir / "operator" / "decision.json").exists()
    assert (run_dir / "operator" / "compiled-plan.json").exists()
    assert (run_dir / "operator" / "gate.json").exists()
    assert not (run_dir / "state.json").exists()


def while_playbook(*, approval=False, condition="loop.iteration < 2", max_iterations=3):
    body = [{
        "id": "emit",
        "type": "action",
        "action": "set",
        "with": {"value": "{{ loop.iteration }}"},
        "requires_approval": approval,
    }]
    return pb({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "bounded-loop", "version": "0.5.0", "description": "bounded loop"},
        "spec": {
            "execution": {"max_loop_iterations": 5},
            "steps": [{
                "id": "refine",
                "type": "while",
                "condition": condition,
                "max_iterations": max_iterations,
                "do": body,
            }],
            "outputs": {"loop": "{{ steps.refine.output }}"},
        },
    })


def test_bounded_while_loop_runs_until_condition_false(tmp_path):
    plan = Compiler().compile(while_playbook())
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    assert state.status == RunStatus.COMPLETED
    result = state.outputs["loop"]
    assert result["count"] == 2
    assert result["terminated"] == "condition_false"
    assert [x["outputs"]["emit"] for x in result["iterations"]] == [0, 1]


def test_while_approval_is_durable_per_iteration(tmp_path):
    plan = Compiler().compile(while_playbook(approval=True))
    run_dir = tmp_path / "run"
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir))
    assert state.status == RunStatus.WAITING_APPROVAL
    first = set(state.pending_approvals)
    assert first == {"refine/iteration-0000/emit"}
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir, resume=True, approvals=first))
    assert state.status == RunStatus.WAITING_APPROVAL
    second = set(state.pending_approvals)
    assert second == {"refine/iteration-0001/emit"}
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir, resume=True, approvals=second))
    assert state.status == RunStatus.COMPLETED
    assert len(state.approvals) == 2


def test_while_fails_closed_when_iteration_ceiling_exhausted(tmp_path):
    plan = Compiler().compile(while_playbook(condition="True", max_iterations=2))
    state = asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    assert state.status == RunStatus.FAILED
    assert "exhausted max_iterations=2" in state.steps["refine"].error


def test_while_compile_ceiling_is_enforced():
    with pytest.raises(CompileError, match="max_iterations=6 exceeds"):
        Compiler().compile(while_playbook(max_iterations=6))


def test_expression_boolean_short_circuit_guards_optional_values():
    from agent_playbook_os.expressions import safe_eval
    assert safe_eval("x is None or x.value == 1", {"x": None}) is True
    assert safe_eval("x is not None and x.value == 1", {"x": None}) is False


def test_operator_caps_planner_response_size():
    playbook = simple_playbook("oversized")
    response = {
        "mode": "ephemeral", "confidence": "low", "rationale": "x",
        "playbook": playbook.model_dump(mode="json", by_alias=True),
        "metadata": {"padding": "x" * 2000},
    }
    operator = ModelBackedOperator(StaticPlannerRuntime(response), max_planner_response_bytes=512)
    with pytest.raises(ValueError, match="planner response exceeds"):
        asyncio.run(operator.decide("x", PlaybookCatalog()))


def test_persisted_plan_cannot_bypass_stricter_current_host_policy():
    from agent_playbook_os.models import PolicySpec
    from agent_playbook_os.policy import assert_plan_satisfies_host_policy
    from agent_playbook_os.errors import PolicyDenied

    plan = Compiler().compile(simple_playbook(), {"name": "Ada"})
    with pytest.raises(PolicyDenied, match="current host policy"):
        assert_plan_satisfies_host_policy(plan, PolicySpec(allowed_actions=["echo"]))
    # A host policy that does not make the already-compiled policy stricter is accepted.
    assert_plan_satisfies_host_policy(plan, PolicySpec())
