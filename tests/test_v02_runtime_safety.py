import json

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import CompileError, IntegrityError, StepExecutionError
from agent_playbook_os.models import Playbook, RunStatus, RuntimeResult, UsageMetrics
from agent_playbook_os.runtime import MockRuntime, ReferenceRuntime
from agent_playbook_os.store import RunStore


def make_pb(steps, *, inputs=None, execution=None, policy=None, outputs=None):
    return Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "v02-test", "version": "0.2.0", "description": "v02 test"},
        "spec": {
            "inputs": inputs or {},
            "execution": execution or {},
            "policy": policy or {},
            "steps": steps,
            "outputs": outputs or {},
        },
    })


def test_external_retry_requires_idempotency_key():
    pb = make_pb([
        {
            "id": "write",
            "type": "action",
            "action": "set",
            "side_effects": "external",
            "retry": {"max_attempts": 2},
        }
    ], policy={"require_approval_for": []})
    with pytest.raises(CompileError, match="idempotency_key"):
        Compiler().compile(pb)


@pytest.mark.asyncio
async def test_retry_uses_stable_idempotency_key(tmp_path):
    pb = make_pb(
        [{
            "id": "write",
            "type": "action",
            "action": "set",
            "side_effects": "external",
            "idempotency_key": "{{ inputs.key }}",
            "retry": {"max_attempts": 2},
        }],
        inputs={"key": {"type": "string", "required": True}},
        policy={"require_approval_for": []},
    )
    runtime = MockRuntime(action_outputs={"write": [ValueError("transient"), {"ok": True}]})
    state = await Runner(runtime).run(Compiler().compile(pb, {"key": "stable-123"}), tmp_path / "run")
    assert state.status == RunStatus.COMPLETED
    controls = [c[3] for c in runtime.calls if c[1] == "write"]
    assert len(controls) == 2
    assert {c["idempotency_key"] for c in controls} == {"stable-123"}
    assert [c["attempt"] for c in controls] == [1, 2]


@pytest.mark.asyncio
async def test_retry_on_filter_prevents_wrong_retry(tmp_path):
    pb = make_pb([{
        "id": "x",
        "type": "action",
        "action": "set",
        "retry": {"max_attempts": 2, "retry_on": ["TimeoutError"]},
    }])
    runtime = MockRuntime(action_outputs={"x": [ValueError("nope"), {"ok": True}]})
    state = await Runner(runtime).run(Compiler().compile(pb), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.steps["x"].attempts == 1


@pytest.mark.asyncio
async def test_action_call_budget_blocks_before_second_call(tmp_path):
    pb = make_pb(
        [
            {"id": "a", "type": "action", "action": "set", "with": {"value": 1}},
            {"id": "b", "type": "action", "action": "set", "needs": ["a"], "with": {"value": 2}},
        ],
        execution={"budget": {"max_action_calls": 1}},
    )
    runtime = MockRuntime()
    state = await Runner(runtime).run(Compiler().compile(pb), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.usage.action_calls == 1
    assert [c[1] for c in runtime.calls] == ["a"]
    assert "budget would be exceeded" in state.steps["b"].error


@pytest.mark.asyncio
async def test_step_timeout_is_enforced(tmp_path):
    pb = make_pb([{
        "id": "slow",
        "type": "action",
        "action": "sleep",
        "timeout_seconds": 0.01,
        "with": {"seconds": 0.05},
    }])
    state = await Runner(ReferenceRuntime()).run(Compiler().compile(pb), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert "timed out" in state.steps["slow"].error


@pytest.mark.asyncio
async def test_runtime_usage_can_trip_cost_budget(tmp_path):
    pb = make_pb(
        [{"id": "think", "type": "agent", "description": "think"}],
        execution={"budget": {"max_cost_usd": 1.0}},
    )
    runtime = MockRuntime(agent_outputs={
        "think": RuntimeResult(value={"answer": 1}, usage=UsageMetrics(model_calls=1, cost_usd=2.0))
    })
    state = await Runner(runtime).run(Compiler().compile(pb), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.usage.cost_usd == 2.0
    assert state.usage.model_calls == 1


@pytest.mark.asyncio
async def test_parallel_branch_can_reference_local_dependency(tmp_path):
    pb = make_pb([{
        "id": "parallel",
        "type": "parallel",
        "branches": {
            "one": [
                {"id": "a", "type": "action", "action": "set", "with": {"value": 7}},
                {"id": "b", "type": "action", "action": "set", "needs": ["a"], "with": {"value": "{{ steps.a.output }}"}},
            ]
        },
    }], outputs={"out": "{{ steps.parallel.output.one.b }}"})
    state = await Runner(ReferenceRuntime()).run(Compiler().compile(pb), tmp_path / "run")
    assert state.status == RunStatus.COMPLETED
    assert state.outputs["out"] == 7


def test_parallel_branch_external_dependency_must_be_declared_by_parent():
    pb = make_pb([
        {"id": "a", "type": "action", "action": "set", "with": {"value": 7}},
        {
            "id": "p",
            "type": "parallel",
            "branches": {"one": [
                {"id": "b", "type": "action", "action": "set", "needs": ["a"], "with": {"value": "{{ steps.a.output }}"}}
            ]},
        },
    ])
    with pytest.raises(CompileError, match="must declare external"):
        Compiler().compile(pb)


@pytest.mark.asyncio
async def test_parallel_approval_bearing_step_is_durable(tmp_path):
    pb = make_pb([{
        "id": "p",
        "type": "parallel",
        "branches": {"one": [
            {"id": "write", "type": "action", "action": "set", "side_effects": "external", "with": {"value": 7}}
        ]},
    }], outputs={"out": "{{ steps.p.output.one.write }}"})
    plan = Compiler().compile(pb)
    run_dir = tmp_path / "run"
    first = await Runner(ReferenceRuntime()).run(plan, run_dir)
    assert first.status == RunStatus.WAITING_APPROVAL
    assert first.pending_approvals == ["p/one/write"]
    assert first.nested_steps["p/one/write"].status.value == "WAITING_APPROVAL"
    done = await Runner(ReferenceRuntime()).run(plan, run_dir, approvals={"p/one/write"}, resume=True)
    assert done.status == RunStatus.COMPLETED
    assert done.outputs["out"] == 7
    assert done.nested_steps["p/one/write"].status.value == "COMPLETED"


@pytest.mark.asyncio
async def test_continue_failure_can_feed_downstream_context(tmp_path):
    pb = make_pb([
        {"id": "a", "type": "action", "action": "set", "on_fail": "continue"},
        {"id": "b", "type": "action", "action": "set", "needs": ["a"], "with": {"value": "{{ steps.a.error }}"}},
    ], outputs={"err": "{{ steps.b.output }}"})
    runtime = MockRuntime(action_outputs={"a": ValueError("expected failure")})
    state = await Runner(runtime).run(Compiler().compile(pb), tmp_path / "run")
    assert state.status == RunStatus.COMPLETED
    assert "expected failure" in state.outputs["err"]


@pytest.mark.asyncio
async def test_existing_run_directory_is_not_silently_overwritten(tmp_path):
    pb = make_pb([{"id": "a", "type": "action", "action": "set"}])
    plan = Compiler().compile(pb)
    run_dir = tmp_path / "run"
    await Runner(ReferenceRuntime()).run(plan, run_dir)
    with pytest.raises(StepExecutionError, match="already contains"):
        await Runner(ReferenceRuntime()).run(plan, run_dir)


@pytest.mark.asyncio
async def test_resume_rejects_tampered_event_chain(tmp_path):
    pb = make_pb([
        {"id": "approve", "type": "gate", "gate": "human"},
        {"id": "b", "type": "action", "action": "set", "needs": ["approve"]},
    ])
    plan = Compiler().compile(pb)
    run_dir = tmp_path / "run"
    state = await Runner(ReferenceRuntime()).run(plan, run_dir)
    assert state.status == RunStatus.WAITING_APPROVAL
    store = RunStore(run_dir)
    events = store.read_events()
    events[0]["type"] = "tampered"
    store.events_path.write_text("\n".join(json.dumps(x) for x in events) + "\n")
    with pytest.raises(IntegrityError, match="tampered event chain"):
        await Runner(ReferenceRuntime()).run(plan, run_dir, approvals={"approve"}, resume=True)


def test_plan_integrity_detects_tampering(tmp_path):
    pb = make_pb([{"id": "a", "type": "action", "action": "set"}])
    plan = Compiler().compile(pb)
    store = RunStore(tmp_path / "run")
    store.save_plan(plan)
    raw = json.loads(store.plan_path.read_text())
    raw["compiled_at"] = "1999-01-01T00:00:00+00:00"
    store.plan_path.write_text(json.dumps(raw))
    ok, error = store.verify_plan_integrity()
    assert ok is False
    assert "integrity_hash mismatch" in error


def test_artifact_hash_verification(tmp_path):
    store = RunStore(tmp_path / "run")
    ref = store.write_artifact("report", {"ok": True}, produced_by="step")
    assert store.verify_artifacts([ref]) == (True, [])
    path = store.run_dir / ref.uri[5:]
    path.write_text("tampered")
    ok, errors = store.verify_artifacts([ref])
    assert ok is False
    assert "hash mismatch" in errors[0]
