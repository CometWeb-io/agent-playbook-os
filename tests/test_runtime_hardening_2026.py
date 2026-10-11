"""Regression tests for policy, expression, recovery, and queue boundaries.

These are local tests; no cloud/CI infrastructure is required.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.distributed import SQLiteWorkQueue, WorkSubmission, ClaimLost
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import SideEffectOutcomeUnknown
from agent_playbook_os.expressions import safe_eval
from agent_playbook_os.models import InvocationStatus, Playbook, RunStatus
from agent_playbook_os.runtime import CallableRuntime, ReferenceRuntime
from agent_playbook_os.secret_resolver import SecretResolverChain, SecretValue
from agent_playbook_os.storage import SQLiteRunStore
from agent_playbook_os.store import RunStore
from agent_playbook_os.tenancy import TenantFileSecretResolver
from agent_playbook_os.adapters.openai import OpenAIResponsesRuntime
from agent_playbook_os.adapters.anthropic import AnthropicMessagesRuntime


def pb(steps, **spec):
    return Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "hardening", "version": "0.1.0", "description": "Hardening regression"},
        "spec": {"steps": steps, **spec},
    })


@pytest.mark.parametrize("expression", [
    "inputs.message.__class__",
    "inputs.message.__class__.__mro__",
    "inputs['message'].__class__",
    "inputs.data.__class__",
])
def test_expression_cannot_introspect_python_objects(expression):
    with pytest.raises((ValueError, AttributeError)):
        safe_eval(expression, {"inputs": {"message": "text", "data": {}}})


def test_expression_still_reads_nested_plain_data():
    assert safe_eval("inputs.data[0].ready", {"inputs": {"data": [{"ready": True}]}}) is True


@pytest.mark.asyncio
async def test_host_side_effect_classification_cannot_be_downgraded(tmp_path):
    plan = Compiler().compile(pb([
        {"id": "cmd", "type": "action", "action": "command",
         "side_effects": "none", "with": {"argv": ["echo", "test"]}},
    ]))
    result = await Runner(
        ReferenceRuntime(allow_commands=True, allowed_commands={"echo"}),
    ).run(plan, tmp_path / "run")
    assert result.status == RunStatus.FAILED
    assert "cannot downgrade side effects" in result.steps["cmd"].error


def test_parallel_branch_path_must_be_safe():
    with pytest.raises(ValueError, match="unsafe parallel branch"):
        pb([{"id": "p", "type": "parallel", "branches": {"../escape": [
            {"id": "x", "type": "action", "action": "set"}]}}])


@pytest.mark.asyncio
async def test_unknown_material_outcome_cannot_be_skipped(tmp_path):
    async def uncertain(step, inputs, context):
        raise SideEffectOutcomeUnknown("ack unavailable")

    plan = Compiler().compile(pb([
        {"id": "write", "type": "action", "action": "write",
         "side_effects": "local", "on_fail": "skip"},
    ]))
    state = await Runner(CallableRuntime(actions={"write": uncertain})).run(plan, tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", [RunStore, SQLiteRunStore])
async def test_resume_capability_drift_uses_next_event_sequence(tmp_path, store_type):
    plan = Compiler().compile(pb([
        {"id": "review", "type": "gate", "gate": "human"},
    ]))
    root = tmp_path / store_type.backend_id
    first = await Runner(ReferenceRuntime(), store_factory=store_type).run(plan, root)
    assert first.status == RunStatus.WAITING_APPROVAL
    store = store_type(root)
    state = store.load_state()
    state.runtime_capabilities_hash = "sha256:unrelated-runtime"
    store.save_state(state)
    resumed = await Runner(ReferenceRuntime(), store_factory=store_type).run(
        plan, root, resume=True, approvals={"review"}, allow_runtime_change=True,
    )
    assert resumed.status == RunStatus.COMPLETED
    assert store.verify_event_chain() == (True, None)
    events = store.read_events()
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_subplaybook_inherits_parent_policy(tmp_path):
    folder = tmp_path / "playbooks"
    folder.mkdir()
    (folder / "child.yaml").write_text(
        "apiVersion: playbook.agent/v1alpha1\nkind: Playbook\n"
        "metadata:\n  id: child\n  version: 0.1.0\n  description: child\n"
        "spec:\n  steps:\n  - id: denied\n    type: action\n    action: echo\n",
        encoding="utf-8",
    )
    parent = pb([{"id": "child", "type": "playbook", "playbook": "child.yaml"}],
                policy={"allowed_actions": ["set"]})
    source = folder / "parent.yaml"
    source.write_text("# test parent path", encoding="utf-8")
    plan = Compiler().compile(parent, source_path=str(source))
    state = await Runner(ReferenceRuntime()).run(plan, tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert "not in policy allowlist" in state.steps["child"].error


@pytest.mark.asyncio
async def test_subplaybook_does_not_persist_resolved_secret(tmp_path):
    from agent_playbook_os.secret_resolver import MappingSecretResolver
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "child.yaml").write_text(
        "apiVersion: playbook.agent/v1alpha1\nkind: Playbook\n"
        "metadata:\n  id: child\n  version: 0.1.0\n  description: child\n"
        "spec:\n  inputs:\n    key:\n      type: string\n      sensitive: true\n      required: true\n"
        "  steps:\n  - id: echo\n    type: action\n    action: set\n"
        "    with:\n      value: '{{ inputs.key }}'\n",
        encoding="utf-8",
    )
    source = folder / "parent.yaml"
    source.write_text("# source anchor", encoding="utf-8")
    parent = pb([{
        "id": "child", "type": "playbook", "playbook": "child.yaml",
        "with": {"key": "{{ inputs.key }}"},
    }], inputs={"key": {"type": "string", "sensitive": True, "required": True}})
    plan = Compiler().compile(parent, {"key": "secret://KEY"}, source_path=str(source))
    root = tmp_path / "run"
    state = await Runner(
        ReferenceRuntime(),
        secret_resolver=SecretResolverChain([MappingSecretResolver({"KEY": "child-canary"})]),
    ).run(plan, root)
    assert state.status == RunStatus.COMPLETED
    child_plan = (root / "subruns" / "child" / "plan.json").read_text(encoding="utf-8")
    child_state = (root / "subruns" / "child" / "state.json").read_text(encoding="utf-8")
    assert "child-canary" not in child_plan + child_state
    assert "secret://KEY" in child_plan


@pytest.mark.asyncio
async def test_child_approval_resumes_same_durable_run(tmp_path):
    folder = tmp_path / "playbooks"
    folder.mkdir()
    (folder / "child.yaml").write_text(
        "apiVersion: playbook.agent/v1alpha1\nkind: Playbook\n"
        "metadata:\n  id: child\n  version: 0.1.0\n  description: child\n"
        "spec:\n  steps:\n  - id: review\n    type: gate\n    gate: human\n"
        "  - id: finish\n    type: action\n    action: set\n    needs: [review]\n"
        "    with:\n      value: done\n",
        encoding="utf-8",
    )
    source = folder / "parent.yaml"
    source.write_text("# test parent path", encoding="utf-8")
    plan = Compiler().compile(pb([
        {"id": "child", "type": "playbook", "playbook": "child.yaml"}
    ]), source_path=str(source))
    run_dir = tmp_path / "run"
    first = await Runner(ReferenceRuntime()).run(plan, run_dir)
    assert first.status == RunStatus.WAITING_APPROVAL
    assert first.pending_approvals == ["child/review"]
    state = await Runner(ReferenceRuntime()).run(
        plan, run_dir, approvals={"child/review"}, resume=True,
    )
    assert state.status == RunStatus.COMPLETED
    assert state.steps["child"].output == {}
    child_events = RunStore(run_dir / "subruns" / "child").read_events()
    assert sum(e["type"] == "run.created" for e in child_events) == 1
    assert sum(e["type"] == "run.resumed" for e in child_events) == 1


@pytest.mark.asyncio
async def test_tenant_secret_ref_is_resolved_and_redacted(tmp_path):
    secret_root = tmp_path / "secrets"
    target = secret_root / "team" / "default"
    target.mkdir(parents=True)
    (target / "KEY").write_text("demo-secret", encoding="utf-8")
    from agent_playbook_os.distributed import TenantScope
    resolver = SecretResolverChain([TenantFileSecretResolver(secret_root, TenantScope(tenant_id="team"))])
    captured = []

    async def tool(step, inputs, context):
        captured.append(inputs["key"])
        return {"echo": inputs["key"]}

    playbook = pb([{"id": "use", "type": "action", "action": "tool",
                    "with": {"key": "{{ inputs.key }}"}}],
                  inputs={"key": {"type": "string", "required": True, "sensitive": True}})
    plan = Compiler().compile(playbook, {"key": "tenant-secret://KEY"})
    root = tmp_path / "run"
    result = await Runner(CallableRuntime(actions={"tool": tool}), secret_resolver=resolver).run(plan, root)
    assert result.status == RunStatus.COMPLETED
    assert isinstance(captured[0], SecretValue)
    assert str(captured[0]) == "demo-secret"
    assert "demo-secret" not in (root / "state.json").read_text(encoding="utf-8")



@pytest.mark.asyncio
async def test_strict_preflight_rejects_unclassified_host_action(tmp_path):
    plan = Compiler().compile(pb([{"id": "custom", "type": "action", "action": "custom"}]))
    calls = []
    runtime = CallableRuntime(actions={"custom": lambda step, inputs, context: calls.append("called")})
    from agent_playbook_os.errors import CapabilityNegotiationError
    with pytest.raises(CapabilityNegotiationError, match="unclassified"):
        await Runner(runtime, enforce_capabilities=True).run(plan, tmp_path / "blocked")
    assert calls == []
    assert not (tmp_path / "blocked").exists()


@pytest.mark.asyncio
async def test_strict_preflight_accepts_classified_action(tmp_path):
    plan = Compiler().compile(pb([{"id": "custom", "type": "action", "action": "custom"}]))
    runtime = CallableRuntime(
        actions={"custom": lambda step, inputs, context: {"ok": True}},
        action_side_effects={"custom": "none"},
    )
    state = await Runner(runtime, enforce_capabilities=True).run(plan, tmp_path / "allowed")
    assert state.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_worker_refuses_foreign_completed_run(tmp_path):
    from agent_playbook_os.distributed import DistributedWorker, required_capabilities_for_plan, WorkStatus
    plan = Compiler().compile(pb([{"id": "set", "type": "action", "action": "set"}]))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    run_dir = tmp_path / "run"
    await Runner(ReferenceRuntime()).run(plan, run_dir)
    q = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    q.enqueue(WorkSubmission(
        work_id="foreign", run_id="submitted-run", plan_path=str(plan_path),
        run_dir=str(run_dir), required_capabilities=required_capabilities_for_plan(plan),
    ))
    runtime = ReferenceRuntime()
    worker = DistributedWorker(
        queue=q, runtime=runtime,
        runner_factory=lambda **kwargs: Runner(runtime),
        worker_id="worker",
    )
    result = (await worker.run_once())[0]
    assert result.error is not None
    assert "submission identity" in result.error
    assert q.get("foreign").status != WorkStatus.COMPLETED


@pytest.mark.asyncio
async def test_recorded_stream_redacts_secret_without_corrupting_token_counts(tmp_path):
    from agent_playbook_os.recording import RecordingRuntime, ReplayRuntime
    from agent_playbook_os.models import RuntimeStreamEvent, UsageMetrics

    async def handler(step, inputs, context):
        async def generator():
            yield RuntimeStreamEvent(type="usage", usage=UsageMetrics(input_tokens=5, output_tokens=3))
            yield RuntimeStreamEvent(type="result", value={"message": SecretValue("canary", "secret://test")})
        return generator()

    plan = Compiler().compile(pb([{"id": "s", "type": "action", "action": "stream"}],
                                 outputs={"message": "{{ steps.s.output.message }}"}))
    cassette = tmp_path / "stream.jsonl"
    original = await Runner(RecordingRuntime(
        CallableRuntime(actions={"stream": handler}), cassette
    )).run(plan, tmp_path / "one")
    replayed = await Runner(ReplayRuntime(cassette)).run(plan, tmp_path / "two")
    assert original.status == replayed.status == RunStatus.COMPLETED
    assert original.outputs == replayed.outputs == {"message": "<redacted:secret>"}
    assert original.usage.input_tokens == replayed.usage.input_tokens == 5
    assert "canary" not in cassette.read_text(encoding="utf-8")


def _submission(n):
    return WorkSubmission(work_id=f"job-{n}", run_id=f"run-{n}",
                          plan_path="/tmp/plan", run_dir=f"/tmp/run-{n}",
                          required_capabilities=["action:missing"] if n < 260 else ["action:set"])


def test_capability_queue_does_not_starve_items_after_first_page(tmp_path):
    q = SQLiteWorkQueue(tmp_path / "queue.db")
    for i in range(261):
        q.enqueue(_submission(i))
    item = q.claim(worker_id="worker", capabilities={"action:set"})
    assert item is not None
    assert item.work_id == "job-260"


def test_expired_claim_cannot_ack_without_reclaim(tmp_path):
    q = SQLiteWorkQueue(tmp_path / "queue.db")
    q.enqueue(_submission(260))
    item = q.claim(worker_id="worker", capabilities={"action:set"}, visibility_timeout_seconds=0.001)
    assert item is not None
    time.sleep(0.025)
    with pytest.raises(ClaimLost):
        q.ack(item.work_id, item.claim_token, item.fence, {"ok": True})


def test_cancelled_expired_claim_is_finalized(tmp_path):
    q = SQLiteWorkQueue(tmp_path / "queue.db")
    q.enqueue(_submission(260))
    item = q.claim(worker_id="worker", capabilities={"action:set"}, visibility_timeout_seconds=0.001)
    assert item is not None
    q.request_cancel(item.work_id)
    time.sleep(0.025)
    assert q.claim(worker_id="another", capabilities={"action:set"}) is None
    assert q.get(item.work_id).status == "CANCELLED"


@pytest.mark.asyncio
async def test_provider_adapter_rejects_incomplete_responses():
    openai_client = SimpleNamespace(responses=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(status="incomplete", output_text="partial")
    ))
    anthropic_client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: SimpleNamespace(stop_reason="max_tokens", content=[])
    ))
    step = pb([{"id": "agent", "type": "agent"}]).spec.steps[0]
    with pytest.raises(Exception, match="not completed"):
        await OpenAIResponsesRuntime(openai_client, model="demo").execute_agent(step, {}, {})
    with pytest.raises(Exception, match="requires continuation"):
        await AnthropicMessagesRuntime(anthropic_client, model="demo").execute_agent(step, {}, {})
