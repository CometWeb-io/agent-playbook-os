import pytest
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import MockRuntime, ReferenceRuntime
from agent_playbook_os.store import RunStore


def make_pb(steps, outputs=None, deps=None, policy=None):
    return Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1",
        "kind":"Playbook",
        "metadata":{"id":"runner-test","version":"0.1.0","description":"runner test"},
        "spec":{"dependencies":{"skills":deps or {}},"policy":policy or {},"steps":steps,"outputs":outputs or {}}
    })


@pytest.mark.asyncio
async def test_run_and_outputs(tmp_path):
    p = make_pb([
        {"id":"a","type":"action","action":"set","with":{"value":{"x":1}}},
        {"id":"b","type":"action","action":"set","needs":["a"],"with":{"value":"{{ steps.a.output.x }}"}},
    ], outputs={"value":"{{ steps.b.output }}"})
    plan = Compiler().compile(p)
    state = await Runner(ReferenceRuntime()).run(plan, tmp_path / "run")
    assert state.status == RunStatus.COMPLETED
    assert state.outputs == {"value":1}


@pytest.mark.asyncio
async def test_human_gate_pauses_and_resumes(tmp_path):
    p = make_pb([
        {"id":"a","type":"action","action":"set","with":{"value":1}},
        {"id":"approve","type":"gate","gate":"human","needs":["a"]},
        {"id":"b","type":"action","action":"set","needs":["approve"],"with":{"value":2}},
    ])
    plan = Compiler().compile(p)
    run_dir = tmp_path / "run"
    runner = Runner(ReferenceRuntime())
    state = await runner.run(plan, run_dir)
    assert state.status == RunStatus.WAITING_APPROVAL
    state2 = await Runner(ReferenceRuntime()).run(plan, run_dir, approvals={"approve"}, resume=True)
    assert state2.status == RunStatus.COMPLETED
    assert state2.steps["a"].attempts == 1


@pytest.mark.asyncio
async def test_side_effect_policy_requires_approval(tmp_path):
    p = make_pb([
        {"id":"external","type":"action","action":"set","side_effects":"external","with":{"value":1}},
    ])
    plan = Compiler().compile(p)
    state = await Runner(ReferenceRuntime()).run(plan, tmp_path / "run")
    assert state.status == RunStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_gate_condition_fails_run(tmp_path):
    p = make_pb([
        {"id":"a","type":"action","action":"set","with":{"value":{"ok":False}}},
        {"id":"g","type":"gate","gate":"condition","needs":["a"],"condition":"steps.a.output.ok == True"},
    ])
    plan = Compiler().compile(p)
    state = await Runner(ReferenceRuntime()).run(plan, tmp_path / "run")
    assert state.status == RunStatus.FAILED


@pytest.mark.asyncio
async def test_skill_execution_with_mock(tmp_path):
    p = make_pb(
        [{"id":"research","type":"skill","uses":"evidence-researcher"}],
        deps={"evidence-researcher":{"source":"local"}},
        outputs={"x":"{{ steps.research.output.readiness }}"},
    )
    plan = Compiler().compile(p)
    runtime = MockRuntime(skill_outputs={"research":{"readiness":"READY"}})
    state = await Runner(runtime).run(plan, tmp_path / "run")
    assert state.outputs["x"] == "READY"
