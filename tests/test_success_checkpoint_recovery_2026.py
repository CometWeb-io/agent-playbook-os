"""Never repeat a material call that succeeded before its step checkpoint was persisted."""
import json

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import ReconciliationRequired
from agent_playbook_os.models import (
    InvocationStatus, Playbook, RunStatus, StepStatus,
)
from agent_playbook_os.runtime import CallableRuntime
from agent_playbook_os.storage import SQLiteRunStore
from agent_playbook_os.store import RunStore


def plan(layout="top", *, output_schema=None):
    action = {
        "id": "charge", "type": "action", "action": "charge",
        "side_effects": "external", "with": {"value": 7},
        "idempotency_key": "order-123",
    }
    if output_schema:
        action["output_schema"] = output_schema
    wrapper = {
        "top": action,
        "parallel": {"id": "container", "type": "parallel", "branches": {"a": [action]}},
        "foreach": {"id": "container", "type": "foreach", "items": [1], "steps": [action]},
        "while": {
            "id": "container", "type": "while",
            "condition": "loop.iteration < 1", "max_iterations": 2, "do": [action],
        },
    }[layout]
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "orphan-success", "version": "0.1.0", "description": "crash window"},
        "spec": {"policy": {"require_approval_for": []}, "steps": [wrapper]},
    })
    return Compiler().compile(pb)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
@pytest.mark.parametrize("layout", ["top", "parallel", "foreach", "while"])
async def test_crash_after_success_before_step_checkpoint_never_reexecutes(
    tmp_path, backend, layout
):
    calls = []

    async def charge(step, inputs, context):
        calls.append("external-effect")
        return 7

    runtime = CallableRuntime(actions={"charge": charge})
    compiled = plan(layout)
    run_dir = tmp_path / backend.backend_id
    success = await Runner(runtime, store_factory=backend).run(compiled, run_dir)
    assert success.status == RunStatus.COMPLETED
    assert calls == ["external-effect"]

    store = backend(run_dir)
    state = store.load_state()
    invocation = next(x for x in state.invocations if x.step_id.endswith("charge"))
    assert invocation.status == InvocationStatus.SUCCEEDED
    original_hash = invocation.result_hash
    affected = state.steps.get(invocation.step_id) or state.nested_steps[invocation.step_id]
    affected.status = StepStatus.RUNNING
    affected.output = None
    affected.finished_at = None
    if layout != "top":
        state.steps["container"].status = StepStatus.RUNNING
        state.steps["container"].output = None
    state.status = RunStatus.FAILED
    state.finished_at = None
    store.save_state(state)
    assert store.verify_state_integrity() == (True, None)

    with pytest.raises(ReconciliationRequired, match="succeeded without a completed step checkpoint"):
        await Runner(runtime, store_factory=backend).run(compiled, run_dir, resume=True)
    assert calls == ["external-effect"]

    with pytest.raises(ReconciliationRequired, match="cannot be reconciled as not-executed"):
        backend(run_dir).reconcile_invocation(invocation.invocation_id, outcome="not-executed")

    with pytest.raises(ReconciliationRequired, match="does not match"):
        backend(run_dir).reconcile_invocation(
            invocation.invocation_id, outcome="succeeded", output="forged-output"
        )
    assert backend(run_dir).load_state().invocations[-1].status == InvocationStatus.SUCCEEDED
    recovered = backend(run_dir).reconcile_invocation(
        invocation.invocation_id, outcome="succeeded", output=7,
        actor="reviewer-checked-provider",
    )
    assert recovered.invocations[-1].result_hash == original_hash
    assert recovered.invocations[-1].status == InvocationStatus.RECONCILED
    final = await Runner(runtime, store_factory=backend).run(compiled, run_dir, resume=True)
    assert final.status == RunStatus.COMPLETED
    assert calls == ["external-effect"], "reconciliation may not dispatch another charge"
    assert backend(run_dir).verify_event_chain() == (True, None)


@pytest.mark.asyncio
async def test_output_schema_failure_does_not_permit_external_retry_or_schema_bypass(tmp_path):
    schema = tmp_path / "result.json"
    schema.write_text(json.dumps({"type": "string"}))
    source = tmp_path / "playbook.yaml"
    source.write_text("# source root")
    action = {
        "id": "charge", "type": "action", "action": "charge",
        "side_effects": "external", "with": {"value": 7},
        "output_schema": "result.json",
    }
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "schema-crash", "version": "0.1.0", "description": "validation"},
        "spec": {"policy": {"require_approval_for": []}, "steps": [action]},
    })
    compiled = Compiler().compile(pb, source_path=str(source))
    calls = []

    async def charge(step, inputs, context):
        calls.append("external-effect")
        return 7

    runtime = CallableRuntime(actions={"charge": charge})
    root = tmp_path / "run"
    failed = await Runner(runtime).run(compiled, root)
    assert failed.status == RunStatus.FAILED
    assert failed.invocations[0].status == InvocationStatus.SUCCEEDED
    assert calls == ["external-effect"]
    with pytest.raises(ReconciliationRequired, match="succeeded without a completed"):
        await Runner(runtime).run(compiled, root, resume=True)
    RunStore(root).reconcile_invocation(
        failed.invocations[0].invocation_id, outcome="succeeded", output=7
    )
    # Reconstructed output is the same observed result, but schema was not
    # satisfied. The operator cannot turn the original invalid output into an
    # accepted result simply by setting a completed flag.
    with pytest.raises(Exception, match="7 is not of type 'string'"):
        await Runner(runtime).run(compiled, root, resume=True)
    assert calls == ["external-effect"]


@pytest.mark.asyncio
async def test_already_completed_successful_invocation_is_not_reconcilable(tmp_path):
    compiled = plan()
    runner = Runner(CallableRuntime(actions={"charge": lambda s, i, c: 7}))
    root = tmp_path / "run"
    finished = await runner.run(compiled, root)
    assert finished.status == RunStatus.COMPLETED
    with pytest.raises(ReconciliationRequired, match="already has a completed"):
        RunStore(root).reconcile_invocation(
            finished.invocations[0].invocation_id, outcome="succeeded", output=7,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_real_postdispatch_snapshot_failure_blocks_second_external_effect(tmp_path, backend):
    calls = []

    class FailOnceAfterProviderSuccess(backend):
        def save_state(self, state):
            if (
                not getattr(self, "_injected", False)
                and any(x.status == InvocationStatus.SUCCEEDED for x in state.invocations)
                and state.steps["charge"].status == StepStatus.COMPLETED
            ):
                self._injected = True
                raise OSError("simulated persistence interruption after external success")
            return super().save_state(state)

    async def charge(step, inputs, context):
        calls.append("external-effect")
        return 7

    root = tmp_path / backend.backend_id
    runtime = CallableRuntime(actions={"charge": charge})
    compiled = plan()
    state = await Runner(
        runtime, store_factory=FailOnceAfterProviderSuccess
    ).run(compiled, root)
    assert state.status == RunStatus.FAILED
    assert len(calls) == 1
    recorded = backend(root).load_state()
    assert recorded.invocations[-1].status == InvocationStatus.SUCCEEDED
    assert recorded.steps["charge"].status != StepStatus.COMPLETED
    with pytest.raises(ReconciliationRequired, match="succeeded without a completed"):
        await Runner(runtime, store_factory=backend).run(compiled, root, resume=True)
    assert calls == ["external-effect"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_explicit_json_null_output_can_be_reconciled_but_missing_is_rejected(tmp_path, backend):
    async def action(step, inputs, context):
        return None

    compiled = plan()
    root = tmp_path / backend.backend_id
    first = await Runner(
        CallableRuntime(actions={"charge": action}), store_factory=backend
    ).run(compiled, root)
    assert first.status == RunStatus.COMPLETED
    saved = backend(root).load_state()
    inv = saved.invocations[-1]
    assert inv.status == InvocationStatus.SUCCEEDED
    ss = saved.steps["charge"]
    ss.status = StepStatus.RUNNING
    ss.output = None
    saved.status = RunStatus.FAILED
    backend(root).save_state(saved)
    with pytest.raises(ReconciliationRequired, match="requires output"):
        backend(root).reconcile_invocation(inv.invocation_id, outcome="succeeded")
    restored = backend(root).reconcile_invocation(
        inv.invocation_id, outcome="succeeded", output=None
    )
    assert restored.invocations[-1].status == InvocationStatus.RECONCILED
    assert restored.steps["charge"].status == StepStatus.COMPLETED
    resumed = await Runner(
        CallableRuntime(actions={"charge": action}), store_factory=backend
    ).run(compiled, root, resume=True)
    assert resumed.status == RunStatus.COMPLETED


def test_cli_explicit_json_null_is_not_missing_output(tmp_path, capsys):
    import asyncio
    from types import SimpleNamespace
    from agent_playbook_os.cli import cmd_reconcile
    compiled = plan()
    root = tmp_path / "run"
    async def execute():
        return await Runner(CallableRuntime(actions={"charge": lambda s, i, c: None})).run(
            compiled, root
        )
    first = asyncio.run(execute())
    store = RunStore(root)
    saved = store.load_state()
    saved.steps["charge"].status = StepStatus.RUNNING
    saved.status = RunStatus.FAILED
    store.save_state(saved)
    kwargs = dict(
        run_dir=str(root), invocation_id=first.invocations[0].invocation_id,
        outcome="succeeded", actor="human", note=None
    )
    from agent_playbook_os.errors import ReconciliationRequired
    with pytest.raises(ReconciliationRequired, match="requires output"):
        cmd_reconcile(SimpleNamespace(**kwargs, output=None))
    cmd_reconcile(SimpleNamespace(**kwargs, output="null"))
    capsys.readouterr()
    assert store.load_state().steps["charge"].status == StepStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["top", "parallel", "foreach", "while"])
@pytest.mark.parametrize("on_fail", ["skip", "continue"])
async def test_material_invalid_result_cannot_be_suppressed_by_on_fail(
    tmp_path, layout, on_fail
):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "string"}), encoding="utf-8")
    source = tmp_path / "root.yaml"
    source.write_text("# source", encoding="utf-8")
    write = {
        "id": "charge", "type": "action", "action": "charge",
        "side_effects": "external", "on_fail": on_fail,
        "output_schema": "schema.json", "with": {"value": 7},
    }
    wrapped = {
        "top": write,
        "parallel": {"id": "container", "type": "parallel", "branches": {"a": [write]}},
        "foreach": {"id": "container", "type": "foreach", "items": [1], "steps": [write]},
        "while": {
            "id": "container", "type": "while",
            "condition": "loop.iteration < 1",
            "max_iterations": 2, "do": [write],
        },
    }[layout]
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "no-skip-after-effect", "version": "0.1.0",
                     "description": "validation fail closed"},
        "spec": {"policy": {"require_approval_for": []}, "steps": [wrapped]},
    })
    compiled = Compiler().compile(pb, source_path=str(source))
    calls = []

    async def charge(step, inputs, context):
        calls.append("external-effect")
        return 7

    runtime = CallableRuntime(actions={"charge": charge})
    root = tmp_path / "run"
    state = await Runner(runtime).run(compiled, root)
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.SUCCEEDED
    assert calls == ["external-effect"]
    with pytest.raises(ReconciliationRequired):
        await Runner(runtime).run(compiled, root, resume=True)
    assert calls == ["external-effect"]


@pytest.mark.asyncio
async def test_pure_invalid_result_can_still_follow_explicit_skip_policy(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "string"}), encoding="utf-8")
    source = tmp_path / "source.yaml"
    source.write_text("# source", encoding="utf-8")
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "pure-skippable", "version": "0.1.0",
                     "description": "nonmaterial schema failure"},
        "spec": {"steps": [{
            "id": "compute", "type": "action", "action": "set",
            "side_effects": "none", "on_fail": "skip",
            "with": {"value": 7}, "output_schema": "schema.json",
        }]},
    })
    state = await Runner(CallableRuntime(actions={"set": lambda s, i, c: 7})).run(
        Compiler().compile(pb, source_path=str(source)), tmp_path / "pure",
    )
    assert state.status == RunStatus.COMPLETED
    assert state.steps["compute"].status == StepStatus.SKIPPED
