import pytest
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore


@pytest.mark.asyncio
async def test_approval_is_persisted_and_traced(tmp_path):
    pb = Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1","kind":"Playbook",
        "metadata":{"id":"approval-audit","version":"0.2.0","description":"approval audit"},
        "spec":{"steps":[{"id":"approve","type":"gate","gate":"human"}]}
    })
    plan = Compiler().compile(pb)
    run_dir = tmp_path / "run"
    first = await Runner(ReferenceRuntime()).run(plan, run_dir)
    assert first.status == RunStatus.WAITING_APPROVAL
    done = await Runner(ReferenceRuntime()).run(
        plan, run_dir, approvals={"approve"}, resume=True, approval_actor="alice@example.test"
    )
    assert done.status == RunStatus.COMPLETED
    assert len(done.approvals) == 1
    assert done.approvals[0].actor == "alice@example.test"
    events = RunStore(run_dir).read_events()
    approved = [x for x in events if x["type"] == "gate.approved"]
    assert len(approved) == 1
    assert approved[0]["data"]["actor"] == "alice@example.test"
