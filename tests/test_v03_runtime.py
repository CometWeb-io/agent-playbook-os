import asyncio
import hashlib
import json

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import CompileError, LeaseConflict, ReconciliationRequired, StepExecutionError
from agent_playbook_os.models import InvocationRecord, InvocationStatus, Playbook, RunStatus, StepStatus
from agent_playbook_os.recording import RecordingRuntime, ReplayRuntime
from agent_playbook_os.runtime import MockRuntime, ReferenceRuntime
from agent_playbook_os.schema_resolver import MappingSchemaResolver
from agent_playbook_os.store import RunStore
from agent_playbook_os.telemetry import JsonlTelemetrySink


def pb(steps, *, inputs=None, policy=None, outputs=None, execution=None):
    return Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "v03", "version": "0.3.0", "description": "v03 tests"},
        "spec": {
            "inputs": inputs or {},
            "policy": policy or {},
            "execution": execution or {},
            "steps": steps,
            "outputs": outputs or {},
        },
    })


def test_sensitive_input_requires_reference():
    playbook = pb(
        [{"id": "a", "type": "action", "action": "set"}],
        inputs={"token": {"type": "string", "required": True, "sensitive": True}},
    )
    with pytest.raises(CompileError, match="secret reference"):
        Compiler().compile(playbook, {"token": "raw-secret"})
    Compiler().compile(playbook, {"token": "env://API_TOKEN"})


def test_remote_schema_requires_hash_pin():
    playbook = pb([{
        "id": "a", "type": "action", "action": "set", "output_schema": "https://schemas.test/x.json"
    }])
    with pytest.raises(CompileError, match="requires output_schema_hash"):
        Compiler().compile(playbook)


@pytest.mark.asyncio
async def test_pinned_remote_schema_resolver(tmp_path):
    schema = {"type": "integer"}
    payload = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    playbook = pb([{
        "id": "a", "type": "action", "action": "set", "with": {"value": 3},
        "output_schema": "https://schemas.test/x.json", "output_schema_hash": digest,
    }])
    resolver = MappingSchemaResolver({"https://schemas.test/x.json": payload})
    state = await Runner(ReferenceRuntime(), schema_resolver=resolver).run(Compiler().compile(playbook), tmp_path / "run")
    assert state.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancellation_interrupts_running_step(tmp_path):
    playbook = pb([{"id": "slow", "type": "action", "action": "sleep", "with": {"seconds": 2}}])
    plan = Compiler().compile(playbook)
    run_dir = tmp_path / "run"
    task = asyncio.create_task(Runner(ReferenceRuntime()).run(plan, run_dir))
    await asyncio.sleep(0.08)
    RunStore(run_dir).request_cancellation("operator stop", "alice")
    state = await asyncio.wait_for(task, timeout=2)
    assert state.status == RunStatus.CANCELLED
    assert state.steps["slow"].status == StepStatus.CANCELLED
    assert state.cancellation_reason == "operator stop"


def test_run_lease_prevents_two_runners(tmp_path):
    run_dir = tmp_path / "run"
    store = RunStore(run_dir)
    token = store.acquire_lease("first")
    try:
        with pytest.raises(LeaseConflict, match="already leased"):
            RunStore(run_dir).acquire_lease("second")
    finally:
        store.release_lease(token)


@pytest.mark.asyncio
async def test_record_and_strict_replay(tmp_path):
    playbook = pb(
        [{"id": "a", "type": "action", "action": "set", "with": {"value": {"x": 1}}}],
        outputs={"x": "{{ steps.a.output.x }}"},
    )
    plan = Compiler().compile(playbook)
    cassette = tmp_path / "cassette.jsonl"
    first = await Runner(RecordingRuntime(ReferenceRuntime(), cassette)).run(plan, tmp_path / "a")
    second = await Runner(ReplayRuntime(cassette)).run(plan, tmp_path / "b")
    assert first.outputs == second.outputs == {"x": 1}


@pytest.mark.asyncio
async def test_runtime_isolation_fails_closed(tmp_path):
    playbook = pb([{
        "id": "agent", "type": "agent", "description": "review", "execution_isolation": "subagent"
    }])
    state = await Runner(MockRuntime()).run(Compiler().compile(playbook), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert "does not support requested isolation=subagent" in state.steps["agent"].error


@pytest.mark.asyncio
async def test_policy_can_require_sandbox_for_external_side_effects(tmp_path):
    playbook = pb(
        [{"id": "write", "type": "action", "action": "set", "side_effects": "external", "with": {"value": 1}}],
        policy={"require_approval_for": [], "require_isolation_for": {"external": "sandbox"}},
    )
    state = await Runner(MockRuntime()).run(Compiler().compile(playbook), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert "requires sandbox isolation" in state.steps["write"].error


@pytest.mark.asyncio
async def test_invocation_journal_records_success(tmp_path):
    playbook = pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 1}}])
    state = await Runner(ReferenceRuntime()).run(Compiler().compile(playbook), tmp_path / "run")
    assert len(state.invocations) == 1
    inv = state.invocations[0]
    assert inv.status == InvocationStatus.SUCCEEDED
    assert inv.step_id == "a"
    assert state.steps["a"].invocation_ids == [inv.invocation_id]


@pytest.mark.asyncio
async def test_uncertain_external_invocation_blocks_resume_until_reconciled(tmp_path):
    playbook = pb([
        {"id": "write", "type": "action", "action": "set", "side_effects": "external", "with": {"value": 1}},
    ], policy={"require_approval_for": []})
    plan = Compiler().compile(playbook)
    run_dir = tmp_path / "run"
    # Create a real run then simulate a crash window: invocation persisted as STARTED
    # while the step is unfinished.
    state = await Runner(ReferenceRuntime()).run(plan, run_dir)
    store = RunStore(run_dir)
    state = store.load_state()
    state.status = RunStatus.FAILED
    state.steps["write"].status = StepStatus.RUNNING
    state.steps["write"].output = None
    state.invocations[0].status = InvocationStatus.STARTED
    state.invocations[0].finished_at = None
    state.invocations[0].result_hash = None
    store.save_state(state)
    with pytest.raises(ReconciliationRequired, match="reconcile before resume"):
        await Runner(ReferenceRuntime()).run(plan, run_dir, resume=True)
    store.reconcile_invocation(state.invocations[0].invocation_id, outcome="succeeded", output=1, actor="ops")
    done = await Runner(ReferenceRuntime()).run(plan, run_dir, resume=True)
    assert done.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_approval_record_survives_failed_run_and_resume(tmp_path):
    playbook = pb([{
        "id": "write", "type": "action", "action": "set", "side_effects": "external", "with": {"value": 1}
    }])
    plan = Compiler().compile(playbook)
    runtime = MockRuntime(action_outputs={"write": [ValueError("first failure"), 1]})
    run_dir = tmp_path / "run"
    failed = await Runner(runtime).run(plan, run_dir, approvals={"write"}, approval_actor="alice")
    assert failed.status == RunStatus.FAILED
    assert [x.actor for x in failed.approvals] == ["alice"]
    done = await Runner(runtime).run(plan, run_dir, resume=True)
    assert done.status == RunStatus.COMPLETED
    assert len(done.approvals) == 1


def test_telemetry_redacts_sensitive_keys_and_exporter_is_separate(tmp_path):
    sink = JsonlTelemetrySink(tmp_path / "telemetry.jsonl")
    sink.emit({"authorization": "Bearer abc", "nested": {"api_key": "secret", "safe": "ok"}})
    record = json.loads((tmp_path / "telemetry.jsonl").read_text())
    assert record["authorization"] == "<redacted>"
    assert record["nested"]["api_key"] == "<redacted>"
    assert record["nested"]["safe"] == "ok"

@pytest.mark.asyncio
async def test_external_timeout_becomes_unknown_and_does_not_retry_without_adapter_idempotency(tmp_path):
    from agent_playbook_os.runtime import CallableRuntime

    async def slow(step, inputs, context):
        await asyncio.sleep(0.2)
        return {"ok": True}

    playbook = pb([{
        "id": "write",
        "type": "action",
        "action": "slow",
        "side_effects": "external",
        "timeout_seconds": 0.01,
        "idempotency_key": "stable",
        "retry": {"max_attempts": 2},
    }], policy={"require_approval_for": []})
    runtime = CallableRuntime(actions={"slow": slow})
    state = await Runner(runtime).run(Compiler().compile(playbook), tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.steps["write"].attempts == 1
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert "SideEffectOutcomeUnknown" in state.steps["write"].error


@pytest.mark.asyncio
async def test_cancelled_external_invocation_requires_reconciliation_before_resume(tmp_path):
    from agent_playbook_os.runtime import CallableRuntime

    async def slow(step, inputs, context):
        await asyncio.sleep(2)
        return {"ok": True}

    playbook = pb([{
        "id": "write",
        "type": "action",
        "action": "slow",
        "side_effects": "external",
    }], policy={"require_approval_for": []})
    plan = Compiler().compile(playbook)
    run_dir = tmp_path / "run"
    task = asyncio.create_task(Runner(CallableRuntime(actions={"slow": slow})).run(plan, run_dir))
    await asyncio.sleep(0.08)
    RunStore(run_dir).request_cancellation("stop")
    cancelled = await asyncio.wait_for(task, 2)
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.invocations[0].status == InvocationStatus.UNKNOWN

    # Clear the cancellation marker to isolate the reconciliation gate.
    RunStore(run_dir).clear_cancellation_request()
    # A CANCELLED run is terminal by default, so simulate an operator choosing to
    # recover the run while preserving the uncertain invocation evidence.
    state = RunStore(run_dir).load_state()
    state.status = RunStatus.FAILED
    RunStore(run_dir).save_state(state)
    with pytest.raises(ReconciliationRequired):
        await Runner(CallableRuntime(actions={"slow": slow})).run(plan, run_dir, resume=True)
