"""Queue approval boundary: queued strings are requests, not human grants."""
import pytest

from agent_playbook_os.cli import build_parser, cmd_queue_enqueue
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.distributed import (
    DistributedWorker, SQLiteWorkQueue, WorkSubmission, WorkStatus,
    required_capabilities_for_plan,
)
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore


def plan_with_gate():
    playbook = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "queue-auth", "version": "0.1.0",
                     "description": "human review is mandatory"},
        "spec": {"steps": [
            {"id": "review", "type": "gate", "gate": "human"},
            {"id": "done", "type": "action", "action": "set",
             "needs": ["review"], "with": {"value": "ok"}},
        ]},
    })
    return Compiler().compile(playbook)


def setup_worker(tmp_path, *, approvals=(), actor="forged-admin",
                 authorizer=None, max_attempts=1):
    plan = plan_with_gate()
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    run_dir = tmp_path / "run"
    queue = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(WorkSubmission(
        work_id="work", run_id="submitted-run", plan_path=str(path),
        run_dir=str(run_dir), approvals=list(approvals),
        approval_actor=actor, max_attempts=max_attempts,
        required_capabilities=required_capabilities_for_plan(plan),
    ))
    runtime = ReferenceRuntime()

    def runner_factory(store_backend="filesystem", item=None):
        return Runner(runtime, store_factory=RunStore, enforce_capabilities=True)

    worker = DistributedWorker(
        queue=queue, runtime=runtime, runner_factory=runner_factory,
        approval_authorizer=authorizer, worker_id="worker", visibility_timeout_seconds=2,
    )
    return worker, queue, run_dir


@pytest.mark.asyncio
async def test_queue_strings_never_grant_approval_by_default(tmp_path):
    worker, queue, run_dir = setup_worker(tmp_path, approvals={"review"})
    results = await worker.run_once()
    assert len(results) == 1
    assert "untrusted" in results[0].error
    assert queue.get("work").status == WorkStatus.DEAD
    assert not (run_dir / "state.json").exists()


@pytest.mark.asyncio
async def test_without_grant_worker_stops_at_human_gate(tmp_path):
    worker, queue, run_dir = setup_worker(tmp_path)
    results = await worker.run_once()
    assert results[0].error is None
    assert results[0].state_status == RunStatus.WAITING_APPROVAL.value
    assert queue.get("work").status == WorkStatus.WAITING_APPROVAL
    state = RunStore(run_dir).load_state()
    assert state.pending_approvals == ["review"]
    assert not state.approvals


@pytest.mark.asyncio
async def test_host_authorizer_can_grant_approval_with_authenticated_actor(tmp_path):
    def authorizer(item, plan):
        assert plan.playbook_id == "queue-auth"
        assert item.tenant_id == "default"
        return {"review"}, "reviewer@example.test"

    worker, queue, run_dir = setup_worker(
        tmp_path, approvals={"review"}, actor="spoofed-admin", authorizer=authorizer
    )
    results = await worker.run_once()
    assert results[0].error is None
    assert queue.get("work").status == WorkStatus.COMPLETED
    state = RunStore(run_dir).load_state()
    assert state.status == RunStatus.COMPLETED
    assert len(state.approvals) == 1
    assert state.approvals[0].step_id == "review"
    assert state.approvals[0].actor == "reviewer@example.test"


@pytest.mark.asyncio
async def test_host_authorizer_must_cover_all_requested_ids(tmp_path):
    worker, queue, run_dir = setup_worker(
        tmp_path, approvals={"review"}, authorizer=lambda item, plan: (set(), "reviewer")
    )
    result = (await worker.run_once())[0]
    assert "lack host authorization" in result.error
    assert queue.get("work").status == WorkStatus.DEAD
    assert not (run_dir / "state.json").exists()


@pytest.mark.asyncio
async def test_malformed_host_authorizer_fails_closed(tmp_path):
    worker, queue, run_dir = setup_worker(
        tmp_path, approvals={"review"}, authorizer=lambda item, plan: ({"review"}, "")
    )
    result = (await worker.run_once())[0]
    assert "approval_authorizer" in result.error
    assert not (run_dir / "state.json").exists()


def test_queue_cli_rejects_unauthenticated_approve_flag(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "queue", "enqueue", str(tmp_path / "missing.json"),
        "--queue-db", str(tmp_path / "queue.sqlite3"),
        "--approve", "review",
    ])
    with pytest.raises(SystemExit, match="not an authorization mechanism"):
        cmd_queue_enqueue(args)
