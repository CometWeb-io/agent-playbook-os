"""Provider receipt IDs are immutable evidence across streaming and regular results."""
import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import ProviderEvidenceConflict, ReconciliationRequired
from agent_playbook_os.models import (
    Playbook, ProviderReceipt, RuntimeResult, RuntimeStreamEvent,
    InvocationStatus, RunStatus,
)
from agent_playbook_os.runtime import CallableRuntime
from agent_playbook_os.storage import SQLiteRunStore
from agent_playbook_os.store import RunStore


def compile_steps(steps):
    return Compiler().compile(Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "receipt-evidence", "version": "0.1.0", "description": "evidence identity"},
        "spec": {"policy": {"require_approval_for": []}, "steps": steps},
    }))


def receipt(call_id):
    return ProviderReceipt(
        receipt_id="same-receipt-id", provider="provider",
        call_id=call_id, status="completed",
        received_at="2026-10-11T00:00:00+00:00",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_conflicting_runtime_receipts_mark_material_effect_unknown(tmp_path, backend):
    async def handler(step, inputs, context):
        which = "first-call" if step.id == "first" else "forged-second-call"
        return RuntimeResult(value={"ok": True}, provider_receipts=[receipt(which)])

    plan = compile_steps([
        {"id": "first", "type": "action", "action": "remote", "side_effects": "external"},
        {"id": "second", "type": "action", "action": "remote", "side_effects": "external",
         "needs": ["first"], "on_fail": "skip", "retry": {"max_attempts": 2},
         "idempotency_key": "same-run-key"},
    ])
    root = tmp_path / backend.backend_id
    result = await Runner(
        CallableRuntime(actions={"remote": handler}), store_factory=backend
    ).run(plan, root)
    assert result.status == RunStatus.FAILED
    assert result.invocations[0].status == InvocationStatus.SUCCEEDED
    assert result.invocations[1].status == InvocationStatus.UNKNOWN
    assert result.steps["second"].attempts == 1
    assert len(result.provider_receipts) == 1
    assert result.provider_receipts[0].call_id == "first-call"
    assert "conflicting provider receipt" in result.steps["second"].error

    stored = backend(root)
    assert stored.verify_event_chain() == (True, None)
    assert stored.verify_state_integrity() == (True, None)
    with pytest.raises(ReconciliationRequired):
        await Runner(
            CallableRuntime(actions={"remote": handler}), store_factory=backend
        ).run(plan, root, resume=True)


@pytest.mark.asyncio
async def test_stream_conflicting_receipts_preserve_first_and_unknown(tmp_path):
    async def handler(step, inputs, context):
        async def stream():
            yield RuntimeStreamEvent(type="receipt", receipt=receipt("valid-call"))
            yield RuntimeStreamEvent(type="receipt", receipt=receipt("other-call"))
            yield RuntimeStreamEvent(type="result", value=7)
        return stream()

    plan = compile_steps([
        {"id": "stream", "type": "action", "action": "remote",
         "side_effects": "external", "on_fail": "continue"},
    ])
    root = tmp_path / "stream"
    state = await Runner(
        CallableRuntime(actions={"remote": handler}), store_factory=SQLiteRunStore
    ).run(plan, root)
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert len(state.provider_receipts) == 1
    assert state.provider_receipts[0].call_id == "valid-call"
    assert SQLiteRunStore(root).verify_event_chain() == (True, None)


@pytest.mark.asyncio
async def test_identical_repeated_receipt_is_idempotent(tmp_path):
    same = receipt("one-native-call")

    async def handler(step, inputs, context):
        async def stream():
            yield RuntimeStreamEvent(type="receipt", receipt=same)
            yield RuntimeStreamEvent(type="receipt", receipt=same)
            yield RuntimeStreamEvent(type="result", value={"ok": True})
        return stream()

    plan = compile_steps([{
        "id": "stream", "type": "action", "action": "remote",
        "side_effects": "none",
    }])
    state = await Runner(CallableRuntime(actions={"remote": handler})).run(
        plan, tmp_path / "repeated",
    )
    assert state.status == RunStatus.COMPLETED
    assert len(state.provider_receipts) == 1
    assert state.provider_receipts[0].call_id == "one-native-call"
    assert state.invocations[0].provider_call_id == "one-native-call"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["artifact", "evidence"])
async def test_mismatched_durable_reference_ids_fail_closed(tmp_path, kind):
    from agent_playbook_os.models import ArtifactRef, EvidenceRef

    def reference(which):
        if kind == "artifact":
            return ArtifactRef(
                id="stable-content-id", uri=f"memory://{which}",
                content_hash="sha256:" + ("a" if which == 1 else "b") * 64,
            )
        return EvidenceRef(
            id="stable-content-id", uri=f"memory://{which}",
            content_hash="sha256:" + ("a" if which == 1 else "b") * 64,
        )

    async def handler(step, inputs, context):
        ref = reference(1 if step.id == "first" else 2)
        return RuntimeResult(
            value={"ok": True},
            artifacts=[ref] if kind == "artifact" else [],
            evidence_refs=[ref] if kind == "evidence" else [],
        )

    plan = compile_steps([
        {"id": "first", "type": "action", "action": "remote", "side_effects": "external"},
        {"id": "second", "type": "action", "action": "remote",
         "side_effects": "external", "needs": ["first"], "on_fail": "skip"},
    ])
    result = await Runner(CallableRuntime(actions={"remote": handler})).run(
        plan, tmp_path / kind,
    )
    assert result.status == RunStatus.FAILED
    assert result.invocations[-1].status == InvocationStatus.UNKNOWN
    assert f"conflicting {kind} identity" in result.steps["second"].error
    actual = result.artifacts if kind == "artifact" else result.evidence_refs
    assert len(actual) == 1
    assert actual[0].uri == "memory://1"


@pytest.mark.asyncio
async def test_stream_artifact_collision_does_not_overwrite_first_reference(tmp_path):
    from agent_playbook_os.models import ArtifactRef

    async def handler(step, inputs, context):
        async def stream():
            yield RuntimeStreamEvent(
                type="artifact", artifact=ArtifactRef(id="repeated", uri="memory://original")
            )
            yield RuntimeStreamEvent(
                type="artifact", artifact=ArtifactRef(id="repeated", uri="memory://forged")
            )
            yield RuntimeStreamEvent(type="result", value=42)
        return stream()

    plan = compile_steps([{
        "id": "stream", "type": "action", "action": "remote",
        "side_effects": "external", "on_fail": "continue",
    }])
    state = await Runner(CallableRuntime(actions={"remote": handler})).run(
        plan, tmp_path / "collision",
    )
    assert state.status == RunStatus.FAILED
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert len(state.artifacts) == 1
    assert state.artifacts[0].uri == "memory://original"
