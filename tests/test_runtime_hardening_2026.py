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


@pytest.mark.parametrize("status, expected", [
    ("COMPLETED", "COMPLETED"),
    ("FAILED", "FAILED"),
    ("CANCELLED", "CANCELLED"),
    ("WAITING_APPROVAL", "WAITING_APPROVAL"),
    ("WAITING_INPUT", "WAITING_APPROVAL"),
])
def test_queue_ack_reflects_actual_run_outcome(tmp_path, status, expected):
    q = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    q.enqueue(_submission(260))
    claim = q.claim(worker_id="w", capabilities={"action:set"})
    assert claim is not None
    item = q.ack(claim.work_id, claim.claim_token, claim.fence, {"status": status})
    assert item.status == expected
    assert q.get(item.work_id).status == expected
    assert q.claim(worker_id="w", capabilities={"action:set"}) is None


def test_queue_ack_rejects_unknown_run_status(tmp_path):
    q = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    q.enqueue(_submission(260))
    claim = q.claim(worker_id="w", capabilities={"action:set"})
    assert claim is not None
    with pytest.raises(ValueError, match="nonterminal"):
        q.ack(claim.work_id, claim.claim_token, claim.fence, {"status": "RUNNING"})
    assert q.get(claim.work_id).status == "CLAIMED"


@pytest.mark.asyncio
async def test_sqlite_run_events_checkpoint_state_atomically(tmp_path):
    import sqlite3
    from agent_playbook_os.models import Event

    plan = Compiler().compile(pb([{"id": "step", "type": "action", "action": "set"}]))
    root = tmp_path / "run"
    state = await Runner(ReferenceRuntime(), store_factory=SQLiteRunStore).run(plan, root)
    store = SQLiteRunStore(root)
    before_state = store.load_state().model_dump(mode="json")
    before_events = store.read_events()
    assert before_events
    assert store.verify_event_chain() == (True, None)
    state.metadata["new_metadata"] = "must-not-commit"

    # Simulate a DB fault after entering the event/state transaction.
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "CREATE TRIGGER block_checkpoint BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END;"
        )
    event = Event(seq=before_events[-1]["seq"] + 1, run_id=state.run_id, type="test.injected")
    with pytest.raises(sqlite3.IntegrityError, match="injected checkpoint failure"):
        store.append_event_with_state(event, state)
    assert store.read_events() == before_events
    assert store.load_state().model_dump(mode="json") == before_state
    assert store.verify_event_chain() == (True, None)

    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER block_checkpoint")
    store.append_event_with_state(event, state)
    assert store.read_events()[-1]["type"] == "test.injected"
    assert store.load_state().metadata["new_metadata"] == "must-not-commit"
    assert store.verify_event_chain() == (True, None)


@pytest.mark.asyncio
async def test_local_output_schema_is_pinned_at_compile_time(tmp_path):
    import json
    folder = tmp_path / "sources"
    folder.mkdir()
    schema = folder / "result.json"
    schema.write_text(json.dumps({
        "type": "object", "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False
    }), encoding="utf-8")
    source = folder / "main.yaml"
    source.write_text("# source", encoding="utf-8")
    playbook = pb([{"id": "x", "type": "action", "action": "set",
                    "with": {"value": {"ok": True}}, "output_schema": "result.json"}])
    original_hash = playbook.spec.steps[0].output_schema_hash
    plan = Compiler().compile(playbook, source_path=str(source))
    assert plan.steps[0].spec.output_schema_hash.startswith("sha256:")
    assert original_hash is playbook.spec.steps[0].output_schema_hash is None

    good = await Runner(ReferenceRuntime()).run(plan, tmp_path / "good")
    assert good.status == RunStatus.COMPLETED
    schema.write_text(json.dumps({"type": "string"}), encoding="utf-8")
    stale = await Runner(ReferenceRuntime()).run(plan, tmp_path / "stale")
    assert stale.status == RunStatus.FAILED
    assert "hash mismatch" in stale.steps["x"].error


@pytest.mark.asyncio
async def test_openai_completed_function_call_requires_host_handoff():
    from agent_playbook_os.errors import ProviderContinuationRequired
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: {
        "id": "resp_call", "status": "completed",
        "output": [{"type": "function_call", "name": "publish", "call_id": "call_1"}],
        "output_text": "",
    }))
    step = pb([{"id": "a", "type": "agent"}]).spec.steps[0]
    with pytest.raises(ProviderContinuationRequired, match="function_call"):
        await OpenAIResponsesRuntime(client, model="demo").execute_agent(step, {}, {})


@pytest.mark.asyncio
async def test_provider_continuation_must_not_be_retried_blindly(tmp_path):
    from agent_playbook_os.errors import ProviderContinuationRequired
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return {"id": "resp_1", "status": "incomplete", "output_text": "partial"}
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    plan = Compiler().compile(pb([{
        "id": "agent", "type": "agent",
        "retry": {"max_attempts": 2}, "on_fail": "stop"
    }]))
    result = await Runner(OpenAIResponsesRuntime(client, model="demo")).run(plan, tmp_path / "run")
    assert result.status == RunStatus.FAILED
    assert len(calls) == 1
    assert "ProviderContinuationRequired" in result.steps["agent"].error


@pytest.mark.asyncio
async def test_anthropic_refusal_is_not_a_normal_success():
    from agent_playbook_os.errors import ProviderRefusal
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: {
        "id": "msg_refuse", "stop_reason": "refusal",
        "content": [{"type": "text", "text": "I cannot do that."}],
    }))
    step = pb([{"id": "a", "type": "agent"}]).spec.steps[0]
    with pytest.raises(ProviderRefusal):
        await AnthropicMessagesRuntime(client, model="demo").execute_agent(step, {}, {})


@pytest.mark.skipif(__import__("os").name != "posix", reason="secure openat requires POSIX")
def test_tenant_secrets_reject_symlinked_parent_and_leaf(tmp_path):
    from agent_playbook_os.distributed import TenantScope
    root = tmp_path / "tenants"
    normal = root / "a" / "default"
    normal.mkdir(parents=True)
    (normal / "KEY").write_text("expected", encoding="utf-8")
    resolver = TenantFileSecretResolver(root, TenantScope(tenant_id="a"))
    assert resolver.resolve("tenant-secret://KEY") == "expected"

    (normal / "LINK").symlink_to(normal / "KEY")
    with pytest.raises(Exception, match="secure tenant secret lookup"):
        resolver.resolve("tenant-secret://LINK")

    fake_root = tmp_path / "parent"
    fake_root.mkdir()
    (fake_root / "real").mkdir()
    (fake_root / "real" / "default").mkdir()
    (fake_root / "real" / "default" / "KEY").write_text("expected", encoding="utf-8")
    (fake_root / "alias").symlink_to(fake_root / "real", target_is_directory=True)
    second = TenantFileSecretResolver(fake_root, TenantScope(tenant_id="alias"))
    with pytest.raises(Exception, match="secure tenant secret lookup"):
        second.resolve("tenant-secret://KEY")


@pytest.mark.skipif(__import__("os").name != "posix", reason="secure openat requires POSIX")
def test_tenant_secret_file_limits_and_nonregular_files(tmp_path):
    from agent_playbook_os.distributed import TenantScope
    root = tmp_path / "secrets"
    location = root / "a" / "default"
    location.mkdir(parents=True)
    (location / "KEY").write_text("over-limit", encoding="utf-8")
    resolver = TenantFileSecretResolver(root, TenantScope(tenant_id="a"), max_bytes=3)
    with pytest.raises(Exception, match="maximum size"):
        resolver.resolve("tenant-secret://KEY")
    (location / "DIRECTORY").mkdir()
    with pytest.raises(Exception):
        resolver.resolve("tenant-secret://DIRECTORY")


def test_sqlite_workers_compete_without_duplicate_claims(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    q = SQLiteWorkQueue(tmp_path / "competing.sqlite3")
    ids = {f"job-{i}" for i in range(48)}
    for i in range(48):
        q.enqueue(WorkSubmission(
            work_id=f"job-{i}", run_id=f"run-{i}",
            plan_path="/tmp/plan", run_dir=f"/tmp/run-{i}",
            required_capabilities=["action:set"],
        ))

    def consume(worker):
        claimed = []
        while True:
            item = q.claim(worker_id=f"worker-{worker}", capabilities={"action:set"},
                           visibility_timeout_seconds=15)
            if item is None:
                return claimed
            claimed.append(item.work_id)
            q.ack(item.work_id, item.claim_token, item.fence, {"status": "COMPLETED"})

    with ThreadPoolExecutor(max_workers=6) as pool:
        batches = list(pool.map(consume, range(6)))
    flat = [item for batch in batches for item in batch]
    assert len(flat) == len(ids)
    assert set(flat) == ids
    assert len(flat) == len(set(flat))
    assert q.stats().by_status["COMPLETED"] == len(ids)


@pytest.mark.asyncio
async def test_inspection_exposes_continued_failure_without_claiming_business_success(tmp_path):
    from agent_playbook_os.inspection import inspect_run
    plan = Compiler().compile(pb([
        {"id": "missing", "type": "action", "action": "not-installed", "on_fail": "continue"},
        {"id": "after", "type": "action", "action": "set", "needs": ["missing"],
         "with": {"value": "finished"}},
    ]))
    root = tmp_path / "run"
    state = await Runner(ReferenceRuntime()).run(plan, root)
    assert state.status == RunStatus.COMPLETED
    report = inspect_run(root)
    assert "completed_with_failed_steps" in report["risk_flags"]
    assert report["requires_attention"] is True
    assert report["business_outcome_verified"] is False


@pytest.mark.asyncio
async def test_nonterminal_external_provider_outcome_requires_reconciliation(tmp_path):
    from agent_playbook_os.errors import ReconciliationRequired
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return {"id": "resp_partial", "status": "incomplete", "output_text": "partial"}
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    runtime = OpenAIResponsesRuntime(client, model="demo")
    plan = Compiler().compile(pb([
        {"id": "remote", "type": "agent", "side_effects": "external",
         "idempotency_key": "stable", "retry": {"max_attempts": 2},
         "on_fail": "skip"}
    ], policy={"require_approval_for": []}))
    root = tmp_path / "run"
    result = await Runner(runtime).run(plan, root)
    assert result.status == RunStatus.FAILED
    assert result.invocations[0].status == InvocationStatus.UNKNOWN
    assert len(calls) == 1
    with pytest.raises(ReconciliationRequired):
        await Runner(runtime).run(plan, root, resume=True)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_partial_stream_persists_charged_usage_and_provider_receipts(tmp_path, backend):
    from agent_playbook_os.models import RuntimeStreamEvent, UsageMetrics, ProviderReceipt
    from agent_playbook_os.inspection import inspect_run

    async def streaming(step, inputs, context):
        async def stream():
            yield RuntimeStreamEvent(
                type="usage", usage=UsageMetrics(model_calls=1, input_tokens=120, output_tokens=8)
            )
            yield RuntimeStreamEvent(
                type="receipt", receipt=ProviderReceipt(
                    receipt_id="receipt-before-fault", call_id="resp_123", provider="fixture",
                    status="accepted",
                )
            )
            raise RuntimeError("stream disconnected after provider charged tokens")
        return stream()

    plan = Compiler().compile(pb([{"id": "stream", "type": "action", "action": "stream"}]))
    root = tmp_path / backend.backend_id
    state = await Runner(
        CallableRuntime(actions={"stream": streaming}), store_factory=backend
    ).run(plan, root)
    assert state.status == RunStatus.FAILED
    stored = backend(root).load_state()
    assert stored.usage.model_calls == 1
    assert stored.usage.input_tokens == 120
    assert stored.usage.output_tokens == 8
    assert stored.provider_receipts[0].receipt_id == "receipt-before-fault"
    assert stored.invocations[0].provider_call_id == "resp_123"
    report = inspect_run(root)
    assert "unresolved_provider_acknowledgements" in report["risk_flags"]


@pytest.mark.asyncio
async def test_streaming_budget_interrupts_before_next_chunk_and_requires_reconciliation(tmp_path):
    from agent_playbook_os.errors import ReconciliationRequired
    from agent_playbook_os.models import UsageMetrics, RuntimeStreamEvent
    calls = []

    async def expensive(step, inputs, context):
        calls.append("invoke")
        async def stream():
            yield RuntimeStreamEvent(type="usage", usage=UsageMetrics(
                model_calls=1, input_tokens=8, cost_usd=0.03
            ))
            calls.append("chunk-after-budget")
            yield RuntimeStreamEvent(type="result", value={"ok": True})
        return stream()

    plan = Compiler().compile(pb([
        {"id": "external", "type": "action", "action": "expensive",
         "side_effects": "external", "on_fail": "skip"}
    ], execution={"budget": {"max_input_tokens": 5}},
       policy={"require_approval_for": []}))
    runtime = CallableRuntime(
        actions={"expensive": expensive},
        action_side_effects={"expensive": "external"},
    )
    root = tmp_path / "run"
    result = await Runner(runtime).run(plan, root)
    assert result.status == RunStatus.FAILED
    assert result.usage.input_tokens == 8
    assert result.invocations[0].status == InvocationStatus.UNKNOWN
    assert calls == ["invoke"]
    with pytest.raises(ReconciliationRequired):
        await Runner(runtime).run(plan, root, resume=True)


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX flock required")
def test_dead_filesystem_lease_has_only_one_new_owner(tmp_path):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from agent_playbook_os.errors import LeaseConflict
    root = tmp_path / "run"
    seed = RunStore(root)
    seed.acquire_lease("dead-owner", ttl_seconds=0.01)
    raw = json.loads(seed.lease_path.read_text(encoding="utf-8"))
    raw["pid"] = 999999999
    seed.lease_path.write_text(json.dumps(raw), encoding="utf-8")

    def contender(index):
        store = RunStore(root)
        try:
            return store.acquire_lease(f"contender-{index}", ttl_seconds=1)
        except LeaseConflict:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(contender, range(10)))
    winners = [token for token in results if token is not None]
    assert len(winners) == 1
    lease = RunStore(root).lease_status()
    assert lease is not None and lease["live"] is True
    assert lease["token"] == winners[0]
    RunStore(root).release_lease(winners[0])
