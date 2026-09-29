import asyncio
from pathlib import Path
import time

import pytest

from agent_playbook_os.artifacts import ArtifactVerifierRegistry, FilesystemArtifactStore
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.distributed import (
    ClaimLost,
    DistributedWorker,
    QueueConflict,
    SQLiteFencingCoordinator,
    SQLiteWorkQueue,
    TenantScope,
    WorkStatus,
    WorkSubmission,
    distributed_queue_conformance,
    required_capabilities_for_plan,
)
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import ArtifactRef, Playbook, PolicySpec, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore
from agent_playbook_os.tenancy import TenantFileSecretResolver, TenantPolicyResolver, tenant_path


def make_plan(value="ok"):
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "dist-demo", "version": "1.0.0", "description": "distributed demo"},
        "spec": {
            "steps": [{"id": "set", "type": "action", "action": "set", "with": {"value": value}}],
            "outputs": {"value": "{{ steps.set.output }}"},
        },
    })
    return Compiler().compile(pb)


def test_queue_idempotent_submission_and_conflict(tmp_path):
    q = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    sub = WorkSubmission(work_id="stable", run_id="r1", plan_path="p", run_dir="r")
    a = q.enqueue(sub)
    b = q.enqueue(sub)
    assert a.work_id == b.work_id
    with pytest.raises(QueueConflict):
        q.enqueue(sub.model_copy(update={"run_id": "r2"}))


def test_queue_capability_routing_and_fencing(tmp_path):
    q = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    q.enqueue(WorkSubmission(run_id="r", plan_path="p", run_dir="r", required_capabilities=["action:set"]))
    assert q.claim(worker_id="w0", capabilities=set()) is None
    first = q.claim(worker_id="w1", capabilities={"action:set"}, visibility_timeout_seconds=0.01)
    assert first is not None
    time.sleep(0.02)
    second = q.claim(worker_id="w2", capabilities={"action:set"}, visibility_timeout_seconds=1.0)
    assert second is not None
    assert second.fence > first.fence
    with pytest.raises(ClaimLost):
        q.ack(first.work_id, first.claim_token, first.fence, {})
    done = q.ack(second.work_id, second.claim_token, second.fence, {"ok": True})
    assert done.status == WorkStatus.COMPLETED


def test_queue_filters_are_applied_before_limit(tmp_path):
    q = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    for i in range(5):
        q.enqueue(WorkSubmission(work_id=f"a{i}", tenant_id="a", namespace="n", run_id=f"r{i}", plan_path="p", run_dir="r"))
    q.enqueue(WorkSubmission(work_id="target", tenant_id="b", namespace="n", run_id="rt", plan_path="p", run_dir="r"))
    rows = q.list(tenant_id="b", namespace="n", limit=1)
    assert [x.work_id for x in rows] == ["target"]


def test_queue_conformance(tmp_path):
    counter = 0
    def factory():
        nonlocal counter
        counter += 1
        return SQLiteWorkQueue(tmp_path / f"q{counter}.sqlite3")
    report = distributed_queue_conformance(factory)
    assert report["passed"] is True, report


def test_fencing_coordinator_rejects_stale_holder(tmp_path):
    c = SQLiteFencingCoordinator(tmp_path / "fences.sqlite3")
    first = c.acquire("tenant/ns/run/r", "w1", ttl_seconds=0.01)
    time.sleep(0.02)
    second = c.acquire("tenant/ns/run/r", "w2", ttl_seconds=1.0)
    assert second.fence > first.fence
    with pytest.raises(ClaimLost):
        c.assert_current(first)
    c.assert_current(second)
    with pytest.raises(ClaimLost):
        c.release(first)
    c.release(second)


@pytest.mark.asyncio
async def test_worker_executes_claim_and_recovers_completed_run(tmp_path):
    plan = make_plan("distributed")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    run_dir = tmp_path / "run"
    q = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    q.enqueue(WorkSubmission(
        work_id="work-1", run_id="run-1", plan_path=str(plan_path), run_dir=str(run_dir),
        required_capabilities=required_capabilities_for_plan(plan),
    ))
    runtime = ReferenceRuntime()
    def runner_factory(store_backend="filesystem", item=None):
        return Runner(runtime, store_factory=RunStore, enforce_capabilities=True)
    worker = DistributedWorker(queue=q, runtime=runtime, runner_factory=runner_factory, worker_id="w", visibility_timeout_seconds=1.0)
    results = await worker.run_once()
    assert len(results) == 1
    assert results[0].error is None
    assert q.get("work-1").status == WorkStatus.COMPLETED
    state = RunStore(run_dir).load_state()
    assert state.status == RunStatus.COMPLETED
    assert state.metadata["distributed"]["work_id"] == "work-1"

    # Submitting a second work item for the same completed run must not execute it again.
    q.enqueue(WorkSubmission(
        work_id="work-2", run_id="run-1", plan_path=str(plan_path), run_dir=str(run_dir),
        required_capabilities=required_capabilities_for_plan(plan),
    ))
    results2 = await worker.run_once()
    assert results2[0].work.result["recovered"] is True


def test_artifact_store_and_external_verification(tmp_path):
    scope = TenantScope(tenant_id="acme", namespace="prod")
    external = FilesystemArtifactStore(tmp_path / "artifact-root")
    ref = external.put(scope, "report", {"ok": True})
    registry = ArtifactVerifierRegistry(); registry.register(external)
    run_store = RunStore(tmp_path / "run")
    ok, errors = run_store.verify_artifacts([ref], registry, scope)
    assert ok is True, errors
    ok2, errors2 = run_store.verify_artifacts([ref])
    assert ok2 is False
    assert "not verified" in errors2[0]


def test_artifact_tenant_mismatch_fails(tmp_path):
    a = TenantScope(tenant_id="a", namespace="prod")
    b = TenantScope(tenant_id="b", namespace="prod")
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    ref = store.put(a, "x", b"secret")
    ok, err = store.verify(b, ref)
    assert ok is False
    assert "tenant mismatch" in err


def test_tenant_policy_is_monotonic_and_symlink_is_rejected(tmp_path):
    root = tmp_path / "policies"
    (root / "acme" / "prod").mkdir(parents=True)
    (root / "policy.yaml").write_text("max_steps: 10\n", encoding="utf-8")
    (root / "acme" / "policy.yaml").write_text("max_steps: 5\n", encoding="utf-8")
    (root / "acme" / "prod" / "policy.yaml").write_text("max_steps: 3\n", encoding="utf-8")
    policy = TenantPolicyResolver(root).resolve(TenantScope(tenant_id="acme", namespace="prod"), PolicySpec(max_steps=20))
    assert policy.max_steps == 3
    outside = tmp_path / "outside.yaml"; outside.write_text("max_steps: 1\n")
    link = root / "acme" / "prod" / "policy.yaml"
    link.unlink(); link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked policy"):
        TenantPolicyResolver(root).resolve(TenantScope(tenant_id="acme", namespace="prod"))


def test_tenant_secret_rejects_path_separators_and_symlinks(tmp_path):
    scope = TenantScope(tenant_id="acme", namespace="prod")
    d = tenant_path(tmp_path, scope)
    d.mkdir(parents=True)
    (d / "API_KEY").write_text("value\n", encoding="utf-8")
    resolver = TenantFileSecretResolver(tmp_path, scope)
    assert resolver.resolve("tenant-secret://API_KEY") == "value"
    with pytest.raises(Exception):
        resolver.resolve("tenant-secret://../OTHER")
    outside = tmp_path / "outside"; outside.write_text("bad")
    (d / "LINK").symlink_to(outside)
    with pytest.raises(Exception, match="symlink"):
        resolver.resolve("tenant-secret://LINK")

@pytest.mark.asyncio
async def test_worker_rejects_queued_paths_outside_allowed_roots(tmp_path):
    plan = make_plan("x")
    safe_plans = tmp_path / "plans"; safe_plans.mkdir()
    plan_path = safe_plans / "plan.json"; plan_path.write_text(plan.model_dump_json())
    outside_run = tmp_path / "outside" / "run"
    q = SQLiteWorkQueue(tmp_path / "q.sqlite3")
    q.enqueue(WorkSubmission(
        work_id="escape", run_id="r", plan_path=str(plan_path), run_dir=str(outside_run),
        required_capabilities=required_capabilities_for_plan(plan), max_attempts=1,
    ))
    runtime = ReferenceRuntime()
    def runner_factory(store_backend="filesystem", item=None):
        return Runner(runtime, store_factory=RunStore, enforce_capabilities=True)
    worker = DistributedWorker(
        queue=q, runtime=runtime, runner_factory=runner_factory,
        allowed_run_root=tmp_path / "allowed-runs", allowed_plan_root=safe_plans,
        visibility_timeout_seconds=1.0,
    )
    results = await worker.run_once()
    assert results[0].error and "escapes allowed_run_root" in results[0].error
    assert q.get("escape").status == WorkStatus.DEAD


def test_external_artifact_tamper_is_detected(tmp_path):
    scope = TenantScope(tenant_id="acme", namespace="prod")
    external = FilesystemArtifactStore(tmp_path / "artifact-root")
    ref = external.put(scope, "report", "original")
    registry = ArtifactVerifierRegistry(); registry.register(external)
    parsed = ref.uri.split("/")[-1]
    path = tmp_path / "artifact-root" / "acme" / "prod" / "artifacts" / parsed
    path.write_text("tampered", encoding="utf-8")
    ok, errors = RunStore(tmp_path / "run").verify_artifacts([ref], registry, scope)
    assert ok is False
    assert any("hash mismatch" in e for e in errors)
