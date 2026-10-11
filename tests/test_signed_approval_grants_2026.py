"""HMAC grants bind authenticated approvals to a particular plan and work item."""
from functools import lru_cache

import pytest

from agent_playbook_os.approval_grants import (
    ApprovalGrantError, HMACApprovalAuthorizer, SignedApprovalGrant,
)
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.distributed import (
    DistributedWorker, SQLiteWorkQueue, WorkSubmission, WorkStatus,
    required_capabilities_for_plan,
)
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore


KEY_A = b"K" * 32
KEY_B = b"B" * 32


@lru_cache(maxsize=1)
def compiled():
    return Compiler().compile(Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {
            "id": "signed-approval", "version": "1.0.0",
            "description": "independent human authority",
        },
        "spec": {"steps": [
            {"id": "review", "type": "gate", "gate": "human"},
            {"id": "after", "type": "action", "action": "set",
             "needs": ["review"], "with": {"value": "approved"}},
        ]},
    }))


def submission(**overrides):
    args = {
        "work_id": "work-123", "run_id": "run-123",
        "queue_name": "priority", "tenant_id": "acme",
        "namespace": "production", "plan_path": "unused",
        "run_dir": "unused", "approvals": ["review"],
        "approval_actor": "forged-actor",
    }
    args.update(overrides)
    return WorkSubmission(**args)


def signer(*, now=2000000, audience="production", keys=None):
    return HMACApprovalAuthorizer(
        keys or {"primary": KEY_A}, audience=audience,
        clock=lambda: now, clock_skew_seconds=0,
    )


def signed_payload(*, grantor=None, item=None, plan=None):
    grantor = grantor or signer()
    item = item or submission()
    plan = plan or compiled()
    return grantor.issue(
        item, plan, approved_step_ids={"review"},
        actor="authenticated-reviewer", issuer="trusted-approval-service",
        key_id="primary", ttl_seconds=300, nonce="unique-approval-123",
    )


def item_for(grant, **changes):
    request = submission(**changes)
    request.metadata = {"approval_grant": grant}
    return request


def test_grant_roundtrip_and_actor_is_signed_not_queue_claim():
    payload = signed_payload()
    checked = SignedApprovalGrant.model_validate(payload)
    assert checked.schema_version.endswith("/v1")
    steps, actor = signer()(item_for(payload), compiled())
    assert steps == {"review"}
    assert actor == "authenticated-reviewer"


@pytest.mark.parametrize("tamper", [
    {"actor": "admin"},
    {"approved_step_ids": ["review", "unsafe"]},
    {"tenant_id": "other"},
    {"expires_at": 10000000},
    {"plan_integrity_hash": "sha256:" + "f" * 64},
    {"issuer": "fake-issuer"},
    {"key_id": "other-key"},
])
def test_signed_fields_reject_modification(tamper):
    grant = signed_payload()
    grant.update(tamper)
    with pytest.raises(ApprovalGrantError):
        signer()(item_for(grant), compiled())


@pytest.mark.parametrize("change, expected", [
    ({"work_id": "another-work"}, "work_id"),
    ({"run_id": "another-run"}, "run_id"),
    ({"tenant_id": "other"}, "tenant_id"),
    ({"namespace": "staging"}, "namespace"),
    ({"queue_name": "other"}, "queue_name"),
    ({"approvals": ["other"]}, "approved steps"),
])
def test_replay_into_different_work_scope_fails(change, expected):
    grant = signed_payload()
    with pytest.raises(ApprovalGrantError, match=expected):
        signer()(item_for(grant, **change), compiled())


def test_expiry_not_before_and_audience_are_enforced():
    grant = signed_payload()
    with pytest.raises(ApprovalGrantError, match="not yet valid"):
        signer(now=1999999)(item_for(grant), compiled())
    with pytest.raises(ApprovalGrantError, match="expired"):
        signer(now=2000300)(item_for(grant), compiled())
    with pytest.raises(ApprovalGrantError, match="audience"):
        signer(audience="staging")(item_for(grant), compiled())


def test_key_rotation_is_explicit_and_wrong_key_rejected():
    issuer = signer(keys={"primary": KEY_A, "old": KEY_B})
    grant = signed_payload(grantor=issuer)
    assert signer(keys={"primary": KEY_A, "old": KEY_B})(item_for(grant), compiled())[0] == {"review"}
    with pytest.raises(ApprovalGrantError, match="unknown approval signing key"):
        signer(keys={"old": KEY_B})(item_for(grant), compiled())
    with pytest.raises(ApprovalGrantError, match="signature mismatch"):
        signer(keys={"primary": KEY_B})(item_for(grant), compiled())


def test_unsigned_approval_is_rejected_and_unsigned_noop_is_allowed():
    request = submission()
    with pytest.raises(ApprovalGrantError, match="signed approval_grant"):
        signer()(request, compiled())
    empty = submission(approvals=[])
    assert signer()(empty, compiled()) == (set(), "worker")
    empty.metadata = {"approval_grant": signed_payload()}
    with pytest.raises(ApprovalGrantError, match="unexpected"):
        signer()(empty, compiled())


@pytest.mark.parametrize("bad_key", [b"short", b"", "not-bytes"])
def test_weak_or_invalid_key_rejected(bad_key):
    with pytest.raises(ValueError, match="at least 32 bytes"):
        signer(keys={"primary": bad_key})


def test_signed_grant_rejects_duplicate_steps_and_long_ttl():
    with pytest.raises(ValueError, match="duplicate"):
        signer().issue(
            submission(), compiled(), approved_step_ids=["review", "review"],
            actor="reviewer", issuer="host", key_id="primary",
        )
    with pytest.raises(ValueError, match="ttl_seconds"):
        signer().issue(
            submission(), compiled(), approved_step_ids=["review"],
            actor="reviewer", issuer="host", key_id="primary",
            ttl_seconds=86401,
        )


@pytest.mark.asyncio
async def test_real_worker_executes_only_when_grant_matches_all_scope_fields(tmp_path):
    plan = compiled()
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    run_dir = tmp_path / "run"
    queue = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    request = submission(
        plan_path=str(path), run_dir=str(run_dir),
        required_capabilities=required_capabilities_for_plan(plan),
    )
    request.metadata = {"approval_grant": signed_payload(item=request, plan=plan)}
    queue.enqueue(request)
    runtime = ReferenceRuntime()

    worker = DistributedWorker(
        queue=queue, runtime=runtime, worker_id="worker", queue_name="priority",
        approval_authorizer=signer(),
        runner_factory=lambda **kwargs: Runner(runtime, store_factory=RunStore, enforce_capabilities=True),
    )
    results = await worker.run_once()
    assert len(results) == 1 and results[0].error is None
    assert results[0].state_status == RunStatus.COMPLETED.value
    assert queue.get(request.work_id).status == WorkStatus.COMPLETED
    state = RunStore(run_dir).load_state()
    assert state.status == RunStatus.COMPLETED
    assert len(state.approvals) == 1
    assert state.approvals[0].actor == "authenticated-reviewer"


@pytest.mark.asyncio
async def test_forged_grant_causes_no_execution_and_no_run_state(tmp_path):
    plan = compiled()
    path = tmp_path / "plan.json"
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    run_dir = tmp_path / "run"
    queue = SQLiteWorkQueue(tmp_path / "queue.sqlite3")
    request = submission(
        plan_path=str(path), run_dir=str(run_dir),
        required_capabilities=required_capabilities_for_plan(plan),
    )
    malicious = signed_payload(item=request, plan=plan)
    malicious["actor"] = "forged-admin"
    request.metadata = {"approval_grant": malicious}
    queue.enqueue(request)
    runtime = ReferenceRuntime()
    worker = DistributedWorker(
        queue=queue, runtime=runtime,
        approval_authorizer=signer(), worker_id="worker", queue_name="priority",
        runner_factory=lambda **kwargs: Runner(runtime, store_factory=RunStore),
    )
    result = (await worker.run_once())[0]
    assert result.error is not None and "signature mismatch" in result.error
    assert queue.get(request.work_id).status == WorkStatus.PENDING
    assert not (run_dir / "state.json").exists()


def test_queue_cli_requires_immutable_work_identity_for_signed_grants(tmp_path):
    from agent_playbook_os.cli import build_parser, cmd_queue_enqueue
    compiled_plan = compiled()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(compiled_plan.model_dump_json(), encoding="utf-8")
    signed_path = tmp_path / "grant.json"
    signed_path.write_text("{}", encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args([
        "queue", "enqueue", str(plan_path), "--queue-db", str(tmp_path / "q.db"),
        "--approval-grant-file", str(signed_path),
    ])
    with pytest.raises(SystemExit, match="explicit --work-id and --run-id"):
        cmd_queue_enqueue(args)


@pytest.mark.asyncio
async def test_cli_accepts_host_issued_grant_and_worker_verifies_from_key_env(
    tmp_path, monkeypatch, capsys,
):
    import json
    from agent_playbook_os.cli import build_parser, cmd_queue_enqueue, cmd_worker_once_async

    plan = compiled()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    queue_path = tmp_path / "queue.db"
    run_root = tmp_path / "runs"
    request = submission()
    grant = HMACApprovalAuthorizer(
        {"primary": KEY_A}, audience="prod", clock_skew_seconds=0,
    ).issue(
        request, plan, approved_step_ids={"review"},
        actor="reviewer-verified-by-host", issuer="operator-service",
        key_id="primary",
    )
    grant_path = tmp_path / "grant.json"
    grant_path.write_text(json.dumps(grant), encoding="utf-8")
    parser = build_parser()
    enqueue = parser.parse_args([
        "queue", "enqueue", str(plan_path),
        "--queue-db", str(queue_path),
        "--queue-name", "priority",
        "--tenant", "acme", "--namespace", "production",
        "--run-root", str(run_root), "--run-id", "run-123",
        "--work-id", "work-123", "--approval-grant-file", str(grant_path),
    ])
    cmd_queue_enqueue(enqueue)
    printed = capsys.readouterr().out
    assert "<redacted:approval-grant>" in printed
    assert grant["signature"] not in printed
    monkeypatch.setenv("APBOS_TEST_GRANT_KEY", "K" * 32)
    worker = parser.parse_args([
        "worker", "once", "--queue-db", str(queue_path),
        "--queue-name", "priority",
        "--approval-key-env", "APBOS_TEST_GRANT_KEY",
        "--approval-audience", "prod",
        "--allowed-plan-root", str(tmp_path),
        "--allowed-run-root", str(run_root),
    ])
    await cmd_worker_once_async(worker)
    worker_output = capsys.readouterr().out
    assert grant["signature"] not in worker_output
    item = SQLiteWorkQueue(queue_path).get("work-123")
    assert item.status == WorkStatus.COMPLETED
    state = RunStore(run_root / "acme" / "production" / "run-123").load_state()
    assert state.status == RunStatus.COMPLETED
    assert len(state.approvals) == 1
    assert state.approvals[0].actor == "reviewer-verified-by-host"
    assert "approval_grant" not in state.metadata
    assert grant["signature"] not in json.dumps(state.model_dump(mode="json"))
    assert state.metadata["submitted_metadata_hash"].startswith("sha256:")


def test_signed_enqueue_refuses_altered_submission_scope(tmp_path):
    import json
    from agent_playbook_os.cli import build_parser, cmd_queue_enqueue
    plan = compiled()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    grant_path = tmp_path / "grant.json"
    grant_path.write_text(json.dumps(signed_payload()), encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args([
        "queue", "enqueue", str(plan_path),
        "--queue-db", str(tmp_path / "q.db"), "--queue-name", "priority",
        "--tenant", "acme", "--namespace", "production",
        "--run-id", "run-123", "--work-id", "other-work",
        "--approval-grant-file", str(grant_path),
    ])
    with pytest.raises(SystemExit, match="scope differs from submission: work_id"):
        cmd_queue_enqueue(args)
