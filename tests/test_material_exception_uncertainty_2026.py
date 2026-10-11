"""A generic exception cannot prove a dispatched material operation was absent."""
from __future__ import annotations

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import NoSideEffectPerformed, ReconciliationRequired
from agent_playbook_os.models import (
    InvocationStatus, Playbook, RunStatus, StepStatus,
)
from agent_playbook_os.runtime import CallableRuntime
from agent_playbook_os.storage import SQLiteRunStore
from agent_playbook_os.store import RunStore


def make_plan(layout="top", *, effect="external", key=None, retry_on=None):
    step = {
        "id": "write", "type": "action", "action": "write",
        "side_effects": effect,
        "on_fail": "skip",
        "retry": {"max_attempts": 3, "retry_on": retry_on or []},
        "with": {"value": "payload"},
    }
    if key is not None:
        step["idempotency_key"] = key
    top = {
        "top": step,
        "parallel": {"id": "group", "type": "parallel", "branches": {"a": [step]}},
        "foreach": {"id": "group", "type": "foreach", "items": [1], "steps": [step]},
        "while": {
            "id": "group", "type": "while",
            "condition": "loop.iteration < 1", "max_iterations": 2, "do": [step],
        },
    }[layout]
    return Compiler().compile(Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "material-uncertainty", "version": "1.0.0",
                     "description": "Never blindly repeat a material effect"},
        "spec": {"policy": {"require_approval_for": []}, "steps": [top]},
    }))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
@pytest.mark.parametrize("layout", ["top", "parallel", "foreach", "while"])
@pytest.mark.parametrize("exception_type", [ValueError, OSError, RuntimeError])
async def test_generic_exception_after_material_effect_is_unknown_no_retry(
    tmp_path, backend, layout, exception_type
):
    calls = []
    material = tmp_path / "external-effect.log"

    async def write(step, inputs, context):
        calls.append(step.id)
        with material.open("a") as f:
            f.write("effect-performed\n")
        raise exception_type("api_key=secret-should-not-be-in-journal")

    runtime = CallableRuntime(actions={"write": write}, action_side_effects={"write": "external"})
    compiled = make_plan(layout, retry_on=[exception_type.__name__])
    root = tmp_path / backend.backend_id
    result = await Runner(runtime, store_factory=backend).run(compiled, root)
    assert result.status == RunStatus.FAILED
    assert calls == ["write"]
    assert material.read_text().splitlines() == ["effect-performed"]
    assert len(result.invocations) == 1
    assert result.invocations[0].status == InvocationStatus.UNKNOWN
    assert result.invocations[0].attempt == 1
    assert "reconcile before retry" in result.invocations[0].error
    saved = backend(root)
    assert saved.verify_state_integrity() == (True, None)
    assert saved.verify_event_chain() == (True, None)
    assert "secret-should-not-be-in-journal" not in str(saved.read_events())
    assert "secret-should-not-be-in-journal" not in result.model_dump_json()
    with pytest.raises(ReconciliationRequired, match="reconcile before resume"):
        await Runner(runtime, store_factory=backend).run(compiled, root, resume=True)
    assert calls == ["write"], "resume cannot repeat an uncertain effect"


@pytest.mark.asyncio
async def test_host_can_prove_no_effect_and_retry_without_risk(tmp_path):
    calls = []

    async def handler(step, inputs, context):
        calls.append(True)
        if len(calls) == 1:
            raise NoSideEffectPerformed("authoritative preflight rejection")
        return {"done": True}

    runtime = CallableRuntime(actions={"write": handler}, action_side_effects={"write": "external"})
    state = await Runner(runtime).run(make_plan(), tmp_path / "safe-retry")
    assert state.status == RunStatus.COMPLETED
    assert len(calls) == 2
    assert [i.status for i in state.invocations] == [
        InvocationStatus.FAILED, InvocationStatus.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_runtime_idempotency_never_overrides_missing_resolved_key(tmp_path):
    calls = []

    async def handler(step, inputs, context):
        calls.append(context.get("control", {}).get("idempotency_key"))
        raise OSError("transport interrupted after effect")

    runtime = CallableRuntime(
        actions={"write": handler}, action_side_effects={"write": "external"},
        idempotent_actions={"write"},
    )
    # The template exists but resolves to an empty string, which is not an
    # idempotency key on the remote provider.
    state = await Runner(runtime).run(
        make_plan(key="{{ inputs.empty }}"), tmp_path / "blank-key",
    )
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_valid_resolved_idempotency_key_can_retry_when_adapter_guarantees_it(tmp_path):
    seen = set()
    delivered = []

    async def handler(step, inputs, context):
        key = context["control"]["idempotency_key"]
        if key not in seen:
            seen.add(key)
            delivered.append("one-real-effect")
            raise OSError("acknowledgement lost")
        return {"ok": True, "same_effect": key}

    runtime = CallableRuntime(
        actions={"write": handler}, action_side_effects={"write": "external"},
        idempotent_actions={"write"},
    )
    state = await Runner(runtime).run(
        make_plan(key="stable-dedupe-key"), tmp_path / "dedupe",
    )
    assert state.status == RunStatus.COMPLETED
    assert delivered == ["one-real-effect"]
    assert len(state.invocations) == 2
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert state.invocations[1].status == InvocationStatus.SUCCEEDED
