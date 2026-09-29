import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from agent_playbook_os.adapters.anthropic import AnthropicMessagesRuntime
from agent_playbook_os.adapters.openai import OpenAIResponsesRuntime
from agent_playbook_os.attestation import create_attestation_with_signer, verify_attestation_with_signer
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import LeaseConflict, SecretResolutionError, StepExecutionError
from agent_playbook_os.models import (
    Playbook,
    ProviderReceipt,
    RunStatus,
    RuntimeResult,
    RuntimeStreamEvent,
    UsageMetrics,
)
from agent_playbook_os.negotiation import negotiate_runtime
from agent_playbook_os.plugins import PluginRegistry
from agent_playbook_os.runtime import CallableRuntime, ReferenceRuntime
from agent_playbook_os.secret_resolver import (
    EnvSecretResolver,
    FileSecretResolver,
    MappingSecretResolver,
    SecretResolverChain,
    SecretValue,
)
from agent_playbook_os.signing import CallableSigner
from agent_playbook_os.storage import SQLiteRunStore, open_run_store
from agent_playbook_os.store import RunStore
from agent_playbook_os.telemetry import OpenTelemetrySink


def pb(steps, *, inputs=None, outputs=None, policy=None, execution=None):
    return Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "v04", "version": "0.4.0", "description": "v04 tests"},
        "spec": {
            "inputs": inputs or {},
            "steps": steps,
            "outputs": outputs or {},
            "policy": policy or {},
            "execution": execution or {},
        },
    })


@pytest.mark.asyncio
async def test_env_secret_resolver_value_never_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("APBOS_TEST_SECRET", "super-secret-value")
    seen = {}

    async def handler(step, inputs, context):
        seen["value"] = inputs["token"]
        return {"echo": inputs["token"]}

    playbook = pb([{
        "id": "use-secret",
        "type": "action",
        "action": "custom",
        "with": {"token": "env://APBOS_TEST_SECRET"},
    }])
    resolver = SecretResolverChain([EnvSecretResolver()])
    runtime = CallableRuntime(actions={"custom": handler})
    run_dir = tmp_path / "run"
    state = await Runner(runtime, secret_resolver=resolver).run(Compiler().compile(playbook), run_dir)
    assert isinstance(seen["value"], SecretValue)
    assert str(seen["value"]) == "super-secret-value"
    assert state.steps["use-secret"].output == {"echo": "<redacted:secret>"}
    raw = (run_dir / "state.json").read_text()
    assert "super-secret-value" not in raw


def test_file_secret_resolver_is_root_constrained(tmp_path):
    root = tmp_path / "secrets"
    root.mkdir()
    good = root / "api.txt"
    good.write_text("abc\n")
    resolver = FileSecretResolver([root])
    assert str(resolver.resolve(f"file-secret://{good}")) == "abc"
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    with pytest.raises(SecretResolutionError, match="outside configured roots"):
        resolver.resolve(f"file-secret://{outside}")


@pytest.mark.asyncio
async def test_sqlite_store_runs_and_auto_detects(tmp_path):
    playbook = pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 7}}])
    run_dir = tmp_path / "run"
    state = await Runner(ReferenceRuntime(), store_factory=SQLiteRunStore).run(Compiler().compile(playbook), run_dir)
    assert state.status == RunStatus.COMPLETED
    assert state.storage_backend == "sqlite"
    detected = open_run_store(run_dir)
    assert isinstance(detected, SQLiteRunStore)
    assert detected.load_state().steps["a"].output == 7
    assert detected.verify_event_chain() == (True, None)
    assert detected.verify_state_integrity() == (True, None)


def test_filesystem_lease_can_be_renewed_and_stale_lease_reclaimed(tmp_path):
    store = RunStore(tmp_path / "run")
    token = store.acquire_lease("a", ttl_seconds=0.08)
    first = store.lease_status()
    time.sleep(0.03)
    renewed = store.renew_lease(token, ttl_seconds=0.08)
    assert renewed["heartbeat_at"] != first["heartbeat_at"]
    store.release_lease(token)
    token = store.acquire_lease("stale", ttl_seconds=0.02)
    time.sleep(0.04)
    second = RunStore(tmp_path / "run").acquire_lease("new", ttl_seconds=1)
    assert second != token
    RunStore(tmp_path / "run").release_lease(second)


def test_sqlite_lease_conflict_and_renewal(tmp_path):
    a = SQLiteRunStore(tmp_path / "run")
    b = SQLiteRunStore(tmp_path / "run")
    token = a.acquire_lease("a", ttl_seconds=1)
    with pytest.raises(LeaseConflict):
        b.acquire_lease("b", ttl_seconds=1)
    assert a.renew_lease(token)["token"] == token
    a.release_lease(token)


@pytest.mark.asyncio
async def test_provider_receipt_is_persisted_and_linked_to_invocation(tmp_path):
    receipt = ProviderReceipt(provider="test", call_id="call-123", status="completed")
    runtime = CallableRuntime(actions={"remote": lambda s, i, c: RuntimeResult(value={"ok": True}, provider_receipts=[receipt])})
    playbook = pb([{
        "id": "remote",
        "type": "action",
        "action": "remote",
        "side_effects": "external",
        "with": {},
    }], policy={"require_approval_for": []})
    state = await Runner(runtime).run(Compiler().compile(playbook), tmp_path / "run")
    assert state.provider_receipts[0].call_id == "call-123"
    assert receipt.receipt_id in state.steps["remote"].provider_receipt_ids
    inv = state.invocations[0]
    assert inv.provider_call_id == "call-123"
    assert inv.provider == "test"
    assert receipt.receipt_id in inv.provider_receipt_ids


@pytest.mark.asyncio
async def test_streaming_runtime_aggregates_usage_receipt_and_result(tmp_path):
    async def stream_handler(step, inputs, context):
        async def stream():
            yield RuntimeStreamEvent(type="progress", value="half")
            yield RuntimeStreamEvent(type="usage", usage=UsageMetrics(model_calls=1, output_tokens=5))
            yield RuntimeStreamEvent(type="receipt", receipt=ProviderReceipt(provider="x", call_id="stream-1", status="accepted"))
            yield RuntimeStreamEvent(type="result", value={"done": True})
        return stream()

    runtime = CallableRuntime(actions={"stream": stream_handler})
    playbook = pb([{"id": "s", "type": "action", "action": "stream"}])
    run_dir = tmp_path / "run"
    state = await Runner(runtime).run(Compiler().compile(playbook), run_dir)
    assert state.steps["s"].output == {"done": True}
    assert state.usage.model_calls == 1
    assert state.usage.output_tokens == 5
    assert state.provider_receipts[0].call_id == "stream-1"
    event_types = [e["type"] for e in RunStore(run_dir).read_events()]
    assert "runtime.stream.progress" in event_types
    assert "runtime.stream.result" in event_types


@pytest.mark.asyncio
async def test_stream_without_result_fails(tmp_path):
    async def handler(step, inputs, context):
        async def stream():
            yield RuntimeStreamEvent(type="progress", value="only")
        return stream()
    state = await Runner(CallableRuntime(actions={"s": handler})).run(
        Compiler().compile(pb([{"id": "s", "type": "action", "action": "s"}])), tmp_path / "run"
    )
    assert state.status == RunStatus.FAILED
    assert "ended without a result event" in state.steps["s"].error


@pytest.mark.asyncio
async def test_openai_adapter_maps_usage_and_receipt():
    response = SimpleNamespace(
        id="resp_1",
        output_text="hello",
        usage=SimpleNamespace(input_tokens=10, output_tokens=4),
    )
    class Responses:
        def create(self, **kwargs):
            assert kwargs["model"] == "gpt-test"
            return response
    client = SimpleNamespace(responses=Responses())
    runtime = OpenAIResponsesRuntime(client, model="gpt-test")
    step = pb([{"id": "a", "type": "agent", "description": "say hi"}]).spec.steps[0]
    out = await runtime.execute_agent(step, {}, {"control": {"idempotency_key": "k"}})
    assert out.value == "hello"
    assert out.usage.input_tokens == 10
    assert out.provider_receipts[0].provider == "openai"
    assert out.provider_receipts[0].call_id == "resp_1"


@pytest.mark.asyncio
async def test_anthropic_adapter_maps_usage_and_receipt():
    response = SimpleNamespace(
        id="msg_1",
        content=[SimpleNamespace(text="hello")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )
    class Messages:
        async def create(self, **kwargs):
            return response
    client = SimpleNamespace(messages=Messages())
    runtime = AnthropicMessagesRuntime(client, model="claude-test")
    step = pb([{"id": "a", "type": "agent", "description": "say hi"}]).spec.steps[0]
    out = await runtime.execute_agent(step, {}, {"control": {}})
    assert out.value == "hello"
    assert out.usage.model_calls == 1
    assert out.provider_receipts[0].provider == "anthropic"


def test_capability_negotiation_reports_missing_and_optional_receipt_warning():
    plan = Compiler().compile(pb([
        {"id": "a", "type": "action", "action": "missing", "side_effects": "external"},
    ], policy={"require_approval_for": []}))
    report = negotiate_runtime(plan, ReferenceRuntime())
    assert report.passed is False
    assert "action:missing" in report.missing
    assert any("receipt" in x for x in report.warnings)


def test_capability_negotiation_passes_for_reference_action():
    plan = Compiler().compile(pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 1}}]))
    report = negotiate_runtime(plan, ReferenceRuntime())
    assert report.passed is True
    assert not report.missing


def test_plugin_registry_manual_factory():
    registry = PluginRegistry()
    registry.register("runtime", "demo", lambda value=1: {"value": value})
    assert registry.create("runtime", "demo", value=3) == {"value": 3}
    assert any(x.name == "demo" and x.kind == "runtime" for x in registry.descriptors("runtime"))


def test_callable_attestation_signer(tmp_path):
    plan = Compiler().compile(pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 1}}]))
    asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    import hashlib
    secret = b"key"
    def sign(payload):
        return "demo:" + hashlib.sha256(secret + payload).hexdigest()
    def verify(payload, signature):
        return signature == sign(payload)
    signer = CallableSigner("kms:test", sign, verify)
    att = create_attestation_with_signer(tmp_path / "run", signer)
    assert att.signer == "kms:test"
    assert verify_attestation_with_signer(tmp_path / "run", att, signer) == (True, None)


def test_opentelemetry_sink_works_with_injected_tracer():
    spans = []
    class Span:
        def __init__(self, name): self.name=name; self.attrs={}
        def __enter__(self): spans.append(self); return self
        def __exit__(self, *args): return False
        def set_attribute(self, key, value): self.attrs[key]=value
    class Tracer:
        def start_as_current_span(self, name): return Span(name)
    sink = OpenTelemetrySink(Tracer())
    sink.emit({"type": "step.completed", "run_id": "r", "step_id": "s", "seq": 2, "data": {"duration_ms": 4.2}})
    assert spans[0].name.endswith("step.completed")
    assert spans[0].attrs["playbook.run_id"] == "r"
    assert spans[0].attrs["playbook.data.duration_ms"] == 4.2


@pytest.mark.asyncio
async def test_runtime_capability_change_blocks_resume_unless_explicitly_allowed(tmp_path):
    playbook = pb([
        {"id": "gate", "type": "gate", "gate": "human"},
        {"id": "a", "type": "action", "action": "set", "needs": ["gate"], "with": {"value": 1}},
    ])
    plan = Compiler().compile(playbook)
    run_dir = tmp_path / "run"
    paused = await Runner(ReferenceRuntime()).run(plan, run_dir)
    assert paused.status == RunStatus.WAITING_APPROVAL
    changed = CallableRuntime(actions={"set": lambda s, i, c: i.get("value")})
    with pytest.raises(StepExecutionError, match="runtime capabilities changed"):
        await Runner(changed).run(plan, run_dir, resume=True, approvals={"gate"})
    done = await Runner(changed).run(plan, run_dir, resume=True, approvals={"gate"}, allow_runtime_change=True)
    assert done.status == RunStatus.COMPLETED

@pytest.mark.asyncio
async def test_runner_heartbeat_keeps_short_ttl_lease_alive(tmp_path):
    plan = Compiler().compile(pb([{"id": "slow", "type": "action", "action": "sleep", "with": {"seconds": 0.25}}]))
    run_dir = tmp_path / "run"
    task = asyncio.create_task(Runner(
        ReferenceRuntime(), lease_ttl_seconds=0.09, lease_heartbeat_seconds=0.02
    ).run(plan, run_dir))
    await asyncio.sleep(0.14)
    with pytest.raises(LeaseConflict):
        RunStore(run_dir).acquire_lease("intruder", ttl_seconds=0.2)
    done = await task
    assert done.status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_streaming_record_and_replay(tmp_path):
    from agent_playbook_os.recording import RecordingRuntime, ReplayRuntime

    async def handler(step, inputs, context):
        async def stream():
            yield RuntimeStreamEvent(type="progress", value="one")
            yield RuntimeStreamEvent(type="usage", usage=UsageMetrics(tool_calls=1))
            yield RuntimeStreamEvent(type="result", value={"x": 9})
        return stream()

    plan = Compiler().compile(pb([{"id": "s", "type": "action", "action": "stream"}], outputs={"x": "{{ steps.s.output.x }}"}))
    cassette = tmp_path / "stream.jsonl"
    first = await Runner(RecordingRuntime(CallableRuntime(actions={"stream": handler}), cassette)).run(plan, tmp_path / "a")
    second = await Runner(ReplayRuntime(cassette)).run(plan, tmp_path / "b")
    assert first.outputs == second.outputs == {"x": 9}
    assert first.usage.tool_calls == second.usage.tool_calls == 1
    raw = cassette.read_text()
    assert '"stream"' in raw


def test_migrate_v3_state_in_place_with_backup(tmp_path):
    from agent_playbook_os.integrity import run_state_integrity
    from agent_playbook_os.storage import migrate_run_state

    plan = Compiler().compile(pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 1}}]))
    asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    store = RunStore(tmp_path / "run")
    raw = json.loads(store.state_path.read_text())
    raw["schema_version"] = "agent-playbook-os/run-state/v3"
    raw.pop("provider_receipts", None)
    raw.pop("runtime_capabilities_hash", None)
    raw.pop("storage_backend", None)
    for value in raw.get("steps", {}).values():
        value.pop("provider_receipt_ids", None)
    for value in raw.get("nested_steps", {}).values():
        value.pop("provider_receipt_ids", None)
    for value in raw.get("invocations", []):
        value.pop("provider_receipt_ids", None)
        value.pop("provider_call_id", None)
        value.pop("provider", None)
    raw["integrity_hash"] = "pending"
    raw["integrity_hash"] = run_state_integrity(raw)
    store.state_path.write_text(json.dumps(raw))
    result = migrate_run_state(store, actor="test")
    assert result["changed"] is True
    assert result["from"].endswith("/v3")
    assert result["to"].endswith("/v5")
    assert result["backup"].startswith("migrations/")
    assert store.source_state_version().endswith("/v5")
    assert store.verify_state_integrity() == (True, None)
    assert any(e["type"] == "state.migrated" for e in store.read_events())


@pytest.mark.asyncio
async def test_nested_uncertain_idempotent_invocation_can_resume(tmp_path):
    async def remote(step, inputs, context):
        return {"ok": True}

    playbook = pb([{
        "id": "p",
        "type": "parallel",
        "branches": {
            "left": [{
                "id": "write",
                "type": "action",
                "action": "remote",
                "side_effects": "external",
                "idempotency_key": "stable-key",
            }]
        },
    }], policy={"require_approval_for": []})
    plan = Compiler().compile(playbook)
    runtime = CallableRuntime(actions={"remote": remote}, idempotent_actions={"remote"})
    run_dir = tmp_path / "run"
    done = await Runner(runtime).run(plan, run_dir)
    store = RunStore(run_dir)
    state = store.load_state()
    nested_key = "p/left/write"
    state.status = RunStatus.FAILED
    state.steps["p"].status = state.steps["p"].status.RUNNING
    state.steps["p"].output = None
    state.nested_steps[nested_key].status = state.nested_steps[nested_key].status.RUNNING
    state.nested_steps[nested_key].output = None
    inv = next(x for x in state.invocations if x.step_id == nested_key)
    inv.status = inv.status.UNKNOWN
    inv.finished_at = None
    store.save_state(state)
    resumed = await Runner(runtime).run(plan, run_dir, resume=True)
    assert resumed.status == RunStatus.COMPLETED
    assert resumed.nested_steps[nested_key].status.value == "COMPLETED"


def test_inspect_run_exposes_integrity_receipts_and_counts(tmp_path):
    from agent_playbook_os.inspection import inspect_run

    receipt = ProviderReceipt(provider="test", call_id="c1", status="completed")
    runtime = CallableRuntime(actions={"x": lambda s, i, c: RuntimeResult(value=1, provider_receipts=[receipt])})
    plan = Compiler().compile(pb([{"id": "x", "type": "action", "action": "x"}]))
    asyncio.run(Runner(runtime).run(plan, tmp_path / "run"))
    report = inspect_run(tmp_path / "run")
    assert report["integrity"]["events"]["ok"] is True
    assert report["provider_receipts"][0]["call_id"] == "c1"
    assert report["invocation_count"] == 1
    assert report["event_count"] > 0


def test_sqlite_migration_backup_is_database_record(tmp_path):
    from agent_playbook_os.integrity import run_state_integrity
    from agent_playbook_os.storage import migrate_run_state

    plan = Compiler().compile(pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 1}}]))
    asyncio.run(Runner(ReferenceRuntime(), store_factory=SQLiteRunStore).run(plan, tmp_path / "run"))
    store = SQLiteRunStore(tmp_path / "run")
    raw = json.loads(store._get("state"))
    raw["schema_version"] = "agent-playbook-os/run-state/v3"
    raw.pop("provider_receipts", None)
    raw.pop("runtime_capabilities_hash", None)
    raw.pop("storage_backend", None)
    for value in raw.get("steps", {}).values(): value.pop("provider_receipt_ids", None)
    for value in raw.get("invocations", []):
        value.pop("provider_receipt_ids", None); value.pop("provider_call_id", None); value.pop("provider", None)
    raw["integrity_hash"] = "pending"
    raw["integrity_hash"] = run_state_integrity(raw)
    store._set("state", json.dumps(raw))
    result = migrate_run_state(store, actor="test")
    assert result["backup"].startswith("sqlite:state-backup:")
    assert store.source_state_version().endswith("/v5")

@pytest.mark.asyncio
async def test_lease_heartbeat_failure_marks_material_invocation_unknown(tmp_path):
    class FailingRenewStore(RunStore):
        def renew_lease(self, token, *, ttl_seconds=None):
            raise LeaseConflict("simulated distributed lease loss")

    async def slow(step, inputs, context):
        await asyncio.sleep(1)
        return {"ok": True}

    plan = Compiler().compile(pb([{
        "id": "write",
        "type": "action",
        "action": "slow",
        "side_effects": "external",
    }], policy={"require_approval_for": []}))
    runtime = CallableRuntime(actions={"slow": slow})
    state = await Runner(
        runtime,
        store_factory=FailingRenewStore,
        lease_ttl_seconds=0.09,
        lease_heartbeat_seconds=0.02,
    ).run(plan, tmp_path / "run")
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status.value == "UNKNOWN"
    assert "LeaseConflict" in state.steps["write"].error

@pytest.mark.asyncio
async def test_strict_capability_preflight_fails_before_run_directory_side_effects(tmp_path):
    from agent_playbook_os.errors import CapabilityNegotiationError

    plan = Compiler().compile(pb([{"id": "x", "type": "action", "action": "missing"}]))
    run_dir = tmp_path / "run"
    with pytest.raises(CapabilityNegotiationError, match="action:missing"):
        await Runner(ReferenceRuntime(), enforce_capabilities=True).run(plan, run_dir)
    assert not (run_dir / "state.json").exists()


def test_manual_provider_receipt_attach_links_invocation_and_step(tmp_path):
    plan = Compiler().compile(pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 1}}]))
    asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    store = RunStore(tmp_path / "run")
    state = store.load_state()
    inv = state.invocations[0]
    receipt = ProviderReceipt(provider="manual", call_id="native-42", status="completed")
    updated = store.attach_provider_receipt(inv.invocation_id, receipt, actor="ops")
    assert receipt.receipt_id in updated.invocations[0].provider_receipt_ids
    assert receipt.receipt_id in updated.steps["a"].provider_receipt_ids
    assert updated.invocations[0].provider_call_id == "native-42"
    assert any(e["type"] == "provider.receipt_attached" for e in store.read_events())


def test_capability_registry_rejects_duplicate_ids():
    from agent_playbook_os.capabilities import CapabilityDescriptor, CapabilityRegistry
    cap = CapabilityDescriptor(id="action:x", kind="action", description="x")
    with pytest.raises(ValueError, match="duplicate capability id"):
        CapabilityRegistry([cap, cap])


def test_manual_receipt_metadata_is_redacted_before_persistence(tmp_path):
    plan = Compiler().compile(pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 1}}]))
    asyncio.run(Runner(ReferenceRuntime()).run(plan, tmp_path / "run"))
    store = RunStore(tmp_path / "run")
    inv = store.load_state().invocations[0]
    receipt = ProviderReceipt(provider="manual", call_id="native", metadata={"api_key": "secret", "safe": "ok"})
    updated = store.attach_provider_receipt(inv.invocation_id, receipt)
    saved = updated.provider_receipts[-1]
    assert saved.metadata["api_key"] == "<redacted>"
    assert saved.metadata["safe"] == "ok"
    assert "secret" not in store.state_path.read_text()


def test_cli_replay_can_target_sqlite_store(tmp_path):
    import subprocess
    import sys

    plan = Compiler().compile(pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 7}}]))
    source = tmp_path / "source"
    asyncio.run(Runner(ReferenceRuntime()).run(plan, source))
    env = dict(os.environ)
    repo_src = str((__import__('pathlib').Path(__file__).resolve().parents[1] / "src"))
    env["PYTHONPATH"] = repo_src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    out = subprocess.run(
        [
            sys.executable, "-m", "agent_playbook_os.cli", "replay", str(source),
            "--run-root", str(tmp_path / "replays"), "--run-id", "sqlite-copy", "--store", "sqlite",
        ],
        cwd=__import__('pathlib').Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(out.stdout)
    assert payload["status"] == "COMPLETED"
    target = tmp_path / "replays" / "sqlite-copy"
    assert (target / "run.sqlite3").exists()
    assert open_run_store(target).load_state().storage_backend == "sqlite"


def test_benchmark_runner_factory_honors_sqlite_store(tmp_path):
    from agent_playbook_os.benchmark import benchmark_plan

    plan = Compiler().compile(pb([{"id": "a", "type": "action", "action": "set", "with": {"value": 1}}]))
    root = tmp_path / "bench"
    report = asyncio.run(benchmark_plan(
        plan,
        lambda: ReferenceRuntime(),
        repeats=2,
        root=root,
        runner_factory=lambda runtime: Runner(runtime, store_factory=SQLiteRunStore),
    ))
    assert report.succeeded == 2
    assert (root / "run-0000" / "run.sqlite3").exists()
    assert (root / "run-0001" / "run.sqlite3").exists()


@pytest.mark.asyncio
async def test_provider_adapters_do_not_invent_receipts_without_native_id():
    openai_response = SimpleNamespace(output_text="hello", usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    class Responses:
        def create(self, **kwargs):
            return openai_response
    openai_runtime = OpenAIResponsesRuntime(SimpleNamespace(responses=Responses()), model="gpt-test")
    step = pb([{"id": "a", "type": "agent", "description": "say hi"}]).spec.steps[0]
    out = await openai_runtime.execute_agent(step, {}, {"control": {"idempotency_key": "local-only"}})
    assert out.provider_receipts == []

    anthropic_response = SimpleNamespace(
        content=[SimpleNamespace(text="hello")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    class Messages:
        def create(self, **kwargs):
            return anthropic_response
    anthropic_runtime = AnthropicMessagesRuntime(SimpleNamespace(messages=Messages()), model="claude-test")
    out = await anthropic_runtime.execute_agent(step, {}, {"control": {"idempotency_key": "local-only"}})
    assert out.provider_receipts == []


def test_file_secret_relative_refs_resolve_against_roots_and_reject_ambiguity(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"
    (a / "svc").mkdir(parents=True); (b / "svc").mkdir(parents=True)
    (a / "svc" / "token").write_text("alpha")
    resolver = FileSecretResolver([a, b])
    assert str(resolver.resolve("file-secret://svc/token")) == "alpha"
    (b / "svc" / "token").write_text("beta")
    with pytest.raises(SecretResolutionError, match="ambiguous"):
        resolver.resolve("file-secret://svc/token")
    with pytest.raises(SecretResolutionError, match="escapes"):
        resolver.resolve("file-secret://../escape")


def test_plugin_registry_rejects_duplicate_manual_registration():
    registry = PluginRegistry()
    registry.register("runtime", "demo", lambda: 1)
    with pytest.raises(ValueError, match="duplicate manual plugin"):
        registry.register("runtime", "demo", lambda: 2)


def test_migration_registry_rejects_ambiguous_outgoing_path():
    from agent_playbook_os.migrations import MigrationRegistry
    registry = MigrationRegistry()
    registry.register("v1", "v2", lambda x: x)
    with pytest.raises(ValueError, match="ambiguous migration source"):
        registry.register("v1", "v3", lambda x: x)
