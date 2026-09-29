import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import SideEffectOutcomeUnknown, ReconciliationRequired
from agent_playbook_os.models import Playbook, InvocationStatus
from agent_playbook_os.runtime import CallableRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("side_effects", ["none", "local", "external", "destructive"])
async def test_host_reported_unknown_is_durable_and_blocks_resume(tmp_path, side_effects):
    async def uncertain(step, inputs, context):
        raise SideEffectOutcomeUnknown("provider acknowledgement lost")
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "unknown", "version": "0.1.0", "description": "unknown host outcome"},
        "spec": {"policy": {"require_approval_for": []}, "steps": [{
            "id": "write", "type": "action", "action": "write", "side_effects": side_effects,
            "idempotency_key": "stable", "retry": {"max_attempts": 2},
        }]},
    })
    plan = Compiler().compile(pb)
    runtime = CallableRuntime(actions={"write": uncertain})
    state = await Runner(runtime).run(plan, tmp_path / "run")
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert state.steps["write"].attempts == 1
    with pytest.raises(ReconciliationRequired):
        await Runner(runtime).run(plan, tmp_path / "run", resume=True)
