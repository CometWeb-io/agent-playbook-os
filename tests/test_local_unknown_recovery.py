import asyncio

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import ReconciliationRequired, SideEffectOutcomeUnknown
from agent_playbook_os.models import Playbook, InvocationStatus, RunStatus, StepStatus
from agent_playbook_os.runtime import CallableRuntime
from agent_playbook_os.store import RunStore


def make_plan(layout, effect="local", timeout=None):
    write = {"id": "write", "type": "action", "action": "write", "side_effects": effect,
             "retry": {"max_attempts": 2}, "idempotency_key": "stable"}
    if timeout:
        write["timeout_seconds"] = timeout
    wrapped = {
        "top": write,
        "parallel": {"id": "group", "type": "parallel", "branches": {"one": [write]}},
        "foreach": {"id": "group", "type": "foreach", "items": [1], "steps": [write]},
        "while": {"id": "group", "type": "while", "condition": "loop.iteration < 1",
                  "max_iterations": 2, "do": [write]},
    }[layout]
    wrapped["needs"] = ["before"]
    return Compiler().compile(Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "recovery", "version": "0.1.0", "description": "Recovery regression"},
        "spec": {"policy": {"require_approval_for": []}, "steps": [
            {"id": "before", "type": "action", "action": "before"}, wrapped]},
    }))


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["top", "parallel", "foreach", "while"])
@pytest.mark.parametrize("interruption", ["timeout", "cancel", "task-cancel", "unknown"])
async def test_interrupted_local_write_is_unknown_without_retry(tmp_path, layout, interruption):
    started = asyncio.Event()
    calls = []
    target = tmp_path / "material.txt"

    async def before(step, inputs, context):
        calls.append("before")
        return "done"

    async def write(step, inputs, context):
        calls.append("write")
        target.write_text("material change already made")
        started.set()
        if interruption == "unknown":
            raise SideEffectOutcomeUnknown("acknowledgement unavailable")
        await asyncio.Event().wait()

    runtime = CallableRuntime(actions={"before": before, "write": write})
    plan = make_plan(layout, timeout=0.05 if interruption == "timeout" else None)
    run_dir = tmp_path / "run"
    task = asyncio.create_task(Runner(runtime).run(plan, run_dir))
    await asyncio.wait_for(started.wait(), 2)
    if interruption == "cancel":
        RunStore(run_dir).request_cancellation("operator stop")
    elif interruption == "task-cancel":
        task.cancel()
    if interruption == "task-cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 2)
    store = RunStore(run_dir)
    state = store.load_state()
    assert target.read_text() == "material change already made"
    assert calls == ["before", "write"]
    material = [i for i in state.invocations if i.side_effects == "local"]
    assert len(material) == 1
    assert material[0].status == InvocationStatus.UNKNOWN
    assert state.steps["before"].status == StepStatus.COMPLETED
    # Cancelled runs are terminal; explicitly opt into recovery while retaining
    # the invocation journal. This must not grant permission to repeat the write.
    store.clear_cancellation_request()
    state.status = RunStatus.FAILED
    store.save_state(state)
    with pytest.raises(ReconciliationRequired):
        await Runner(runtime).run(plan, run_dir, resume=True)
    assert calls == ["before", "write"]


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["top", "parallel", "foreach", "while"])
async def test_local_started_crash_window_requires_reconciliation(tmp_path, layout):
    calls = []

    async def action(step, inputs, context):
        calls.append(step.id)
        return "done"

    runtime = CallableRuntime(actions={"before": action, "write": action})
    plan = make_plan(layout)
    run_dir = tmp_path / "run"
    state = await Runner(runtime).run(plan, run_dir)
    inv = next(i for i in state.invocations if i.side_effects == "local")
    inv.status = InvocationStatus.STARTED
    inv.finished_at = None
    inv.result_hash = None
    state.status = RunStatus.FAILED
    write_state = (state.steps | state.nested_steps)[inv.step_id]
    write_state.status = StepStatus.RUNNING
    write_state.output = None
    store = RunStore(run_dir)
    store.save_state(state)
    with pytest.raises(ReconciliationRequired):
        await Runner(runtime).run(plan, run_dir, resume=True)
    assert calls == ["before", "write"]
    store.reconcile_invocation(inv.invocation_id, outcome="succeeded", output="done", actor="test")
    done = await Runner(runtime).run(plan, run_dir, resume=True)
    assert done.status == RunStatus.COMPLETED
    assert calls == ["before", "write"]


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["top", "parallel", "foreach", "while"])
async def test_no_effect_cancellation_does_not_invent_uncertainty(tmp_path, layout):
    started = asyncio.Event()

    async def idle(step, inputs, context):
        started.set()
        await asyncio.Event().wait()

    runtime = CallableRuntime(actions={"before": lambda s, i, c: "done", "write": idle})
    run_dir = tmp_path / "run"
    task = asyncio.create_task(Runner(runtime).run(make_plan(layout, effect="none"), run_dir))
    await asyncio.wait_for(started.wait(), 2)
    RunStore(run_dir).request_cancellation("stop read-only work")
    state = await asyncio.wait_for(task, 2)
    assert state.status == RunStatus.CANCELLED
    assert state.invocations[-1].status == InvocationStatus.CANCELLED
    assert not any(i.status == InvocationStatus.UNKNOWN for i in state.invocations)


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["parallel", "foreach", "while"])
async def test_container_timeout_never_retries_uncertain_children(tmp_path, layout):
    calls = []

    async def write(step, inputs, context):
        calls.append("write")
        await asyncio.Event().wait()

    plan = make_plan(layout)
    parent = plan.steps[-1].spec
    parent.timeout_seconds = 0.05
    parent.retry.max_attempts = 2
    # Compile the final source again so plan integrity includes parent settings.
    source = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "parent-timeout", "version": "0.1.0", "description": "Parent timeout"},
        "spec": {"steps": [s.spec.model_dump(by_alias=True) for s in plan.steps]},
    })
    runtime = CallableRuntime(actions={"before": lambda s, i, c: "done", "write": write})
    run_dir = tmp_path / "run"
    plan = Compiler().compile(source)
    state = await Runner(runtime).run(plan, run_dir)
    assert calls == ["write"]
    assert state.steps["group"].attempts == 1
    assert state.invocations[-1].status == InvocationStatus.UNKNOWN
    with pytest.raises(ReconciliationRequired):
        await Runner(runtime).run(plan, run_dir, resume=True)
    assert calls == ["write"]
