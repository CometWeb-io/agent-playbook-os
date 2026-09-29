import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore
from agent_playbook_os.testing import FaultInjectingRuntime, FaultRule


@pytest.mark.asyncio
async def test_failed_run_can_resume_without_repeating_completed_steps(tmp_path):
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "recovery", "version": "0.2.0", "description": "recovery"},
        "spec": {
            "steps": [
                {"id": "a", "type": "action", "action": "set", "with": {"value": 1}},
                {"id": "b", "type": "action", "action": "set", "needs": ["a"], "with": {"value": 2}},
                {"id": "c", "type": "action", "action": "set", "needs": ["b"], "with": {"value": 3}},
            ],
            "outputs": {"value": "{{ steps.c.output }}"},
        },
    })
    plan = Compiler().compile(pb)
    run_dir = tmp_path / "run"
    failing = FaultInjectingRuntime(ReferenceRuntime(), [FaultRule(step_id="b")])
    first = await Runner(failing).run(plan, run_dir)
    assert first.status == RunStatus.FAILED
    assert first.steps["a"].attempts == 1
    assert first.steps["b"].attempts == 1
    assert first.steps["c"].attempts == 0

    resumed = await Runner(ReferenceRuntime()).run(plan, run_dir, resume=True)
    assert resumed.status == RunStatus.COMPLETED
    assert resumed.steps["a"].attempts == 1
    assert resumed.steps["b"].attempts == 2
    assert resumed.steps["c"].attempts == 1
    assert resumed.outputs == {"value": 3}
    assert RunStore(run_dir).verify_event_chain() == (True, None)
