"""Runner instance state must not cross run boundaries."""
import asyncio

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import StepExecutionError
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime, CallableRuntime
from agent_playbook_os.store import RunStore


def plan(action="set"):
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "runner-reuse", "version": "0.1.0", "description": "Runner lifecycle"},
        "spec": {"steps": [
            {"id": "x", "type": "action", "action": action,
             "with": {"value": 7}},
        ]},
    })
    return Compiler().compile(pb)


@pytest.mark.asyncio
async def test_reused_runner_starts_each_fresh_event_chain_at_one(tmp_path):
    runner = Runner(ReferenceRuntime())
    compiled = plan()
    for run_id in ("first", "second", "third"):
        directory = tmp_path / run_id
        done = await runner.run(compiled, directory)
        assert done.status == RunStatus.COMPLETED
        store = RunStore(directory)
        assert store.verify_event_chain() == (True, None)
        events = store.read_events()
        assert events and events[0]["seq"] == 1
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_concurrent_reuse_of_runner_fails_before_tenant_data_creation(tmp_path):
    started = asyncio.Event()
    allow_finish = asyncio.Event()

    async def slow(step, inputs, context):
        started.set()
        await allow_finish.wait()
        return 7

    runner = Runner(CallableRuntime(actions={"slow": slow}))
    compiled = plan("slow")
    first = asyncio.create_task(runner.run(compiled, tmp_path / "first"))
    await asyncio.wait_for(started.wait(), 2)
    with pytest.raises(StepExecutionError, match="already executing"):
        await runner.run(compiled, tmp_path / "second")
    assert not (tmp_path / "second").exists()
    allow_finish.set()
    first_result = await asyncio.wait_for(first, 3)
    assert first_result.status == RunStatus.COMPLETED

    # It is safe to run again when the first invocation truly terminated.
    second = await runner.run(compiled, tmp_path / "third")
    assert second.status == RunStatus.COMPLETED
    assert RunStore(tmp_path / "third").verify_event_chain() == (True, None)


@pytest.mark.asyncio
async def test_runner_is_released_after_execution_raises(tmp_path):
    runner = Runner(ReferenceRuntime())
    compiled = plan()
    await runner.run(compiled, tmp_path / "first")
    with pytest.raises(StepExecutionError, match="already contains run data"):
        await runner.run(compiled, tmp_path / "first")
    result = await runner.run(compiled, tmp_path / "next")
    assert result.status == RunStatus.COMPLETED
    assert RunStore(tmp_path / "next").verify_event_chain() == (True, None)
