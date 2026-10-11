"""Recovery must never infer retry authorization from a key alone."""
import pytest
import json

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import (
    ProviderContinuationRequired, ProviderEvidenceConflict,
    ReconciliationRequired, SideEffectOutcomeUnknown,
)
from agent_playbook_os.models import InvocationStatus, Playbook, RunStatus
from agent_playbook_os.runtime import CallableRuntime
from agent_playbook_os.storage import SQLiteRunStore
from agent_playbook_os.store import RunStore


def plan(*, attempts=2, retry_on=None):
    return Compiler().compile(Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "retry-eligibility", "version": "0.1.0",
                     "description": "explicit durable retry eligibility"},
        "spec": {
            "execution": {"max_attempts": 3},
            "policy": {"require_approval_for": []},
            "steps": [{
                "id": "write", "type": "action", "action": "write",
                "side_effects": "external",
                "idempotency_key": "dedupe-key",
                "retry": {"max_attempts": attempts, "retry_on": retry_on or []},
            }],
        },
    }))


def runtime(handler):
    return CallableRuntime(
        actions={"write": handler}, action_side_effects={"write": "external"},
        idempotent_actions={"write"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
@pytest.mark.parametrize("kind", ["continuation", "evidence_conflict"])
async def test_classified_manual_only_outcomes_cannot_be_auto_retried(
    tmp_path, backend, kind
):
    count = []

    async def handler(step, inputs, context):
        count.append(1)
        if kind == "continuation":
            raise ProviderContinuationRequired("remote tool call awaits host response")
        if kind == "evidence_conflict":
            raise ProviderEvidenceConflict("conflicting provider receipt evidence")
        return "ok"

    root = tmp_path / backend.backend_id
    rt = runtime(handler)
    initial = await Runner(rt, store_factory=backend).run(plan(), root)
    assert initial.status == RunStatus.FAILED
    assert count == [1]
    inv = initial.invocations[0]
    assert inv.status == InvocationStatus.UNKNOWN
    assert inv.retry_eligible is False
    with pytest.raises(ReconciliationRequired, match="reconcile before resume"):
        await Runner(rt, store_factory=backend).run(plan(), root, resume=True)
    assert count == [1]
    assert backend(root).verify_state_integrity() == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [RunStore, SQLiteRunStore])
async def test_retry_budget_exhaustion_blocks_auto_resume(tmp_path, backend):
    count = []
    async def handler(step, inputs, context):
        count.append(1)
        raise SideEffectOutcomeUnknown("provider acknowledgement unavailable")

    compiled = plan(attempts=2)
    rt = runtime(handler)
    root = tmp_path / backend.backend_id
    failed = await Runner(rt, store_factory=backend).run(compiled, root)
    assert failed.status == RunStatus.FAILED
    assert len(count) == 2
    assert failed.invocations[-1].status == InvocationStatus.UNKNOWN
    assert failed.invocations[-1].retry_eligible is True
    with pytest.raises(ReconciliationRequired):
        await Runner(rt, store_factory=backend).run(compiled, root, resume=True)
    assert len(count) == 2


@pytest.mark.asyncio
async def test_retry_allowlist_filters_resume_not_only_same_run(tmp_path):
    calls = []
    async def handler(step, inputs, context):
        calls.append(1)
        raise SideEffectOutcomeUnknown("ambiguous completion")

    rt = runtime(handler)
    compiled = plan(attempts=2, retry_on=["ValueError"])
    root = tmp_path / "run"
    first = await Runner(rt).run(compiled, root)
    assert first.status == RunStatus.FAILED
    assert first.invocations[0].retry_eligible is False
    with pytest.raises(ReconciliationRequired):
        await Runner(rt).run(compiled, root, resume=True)
    assert calls == [1]


@pytest.mark.asyncio
async def test_resumed_retry_can_be_authorized_when_budget_remains(tmp_path):
    calls = []
    async def handler(step, inputs, context):
        calls.append(context["control"]["idempotency_key"])
        if len(calls) == 1:
            raise SideEffectOutcomeUnknown("ack lost")
        return "deduped"

    class StopAfterFirst(Runner):
        def _should_retry(self, step, exc, *, resolved_key=None):
            return False

    root = tmp_path / "run"
    rt = runtime(handler)
    compiled = plan(attempts=2)
    first = await StopAfterFirst(rt).run(compiled, root)
    assert first.status == RunStatus.FAILED
    assert first.invocations[0].retry_eligible is True
    finished = await Runner(rt).run(compiled, root, resume=True)
    assert finished.status == RunStatus.COMPLETED
    assert calls == ["dedupe-key", "dedupe-key"]
    assert finished.invocations[0].status == InvocationStatus.RECONCILED


@pytest.mark.asyncio
async def test_legacy_unknown_without_durable_retry_eligibility_is_manual(tmp_path):
    calls = []
    async def handler(step, inputs, context):
        calls.append(1)
        raise SideEffectOutcomeUnknown("uncertain")

    class StopAfterFirst(Runner):
        def _should_retry(self, step, exc, *, resolved_key=None):
            return False

    root = tmp_path / "run"
    rt = runtime(handler)
    compiled = plan(attempts=2)
    initial = await StopAfterFirst(rt).run(compiled, root)
    assert initial.invocations[0].retry_eligible is True
    store = RunStore(root)
    state = store.load_state()
    raw = state.model_dump(mode="json")
    raw["invocations"][0].pop("retry_eligible")
    from agent_playbook_os.models import RunState
    store.save_state(RunState.model_validate(raw))
    restored = store.load_state()
    assert restored.invocations[0].retry_eligible is False
    with pytest.raises(ReconciliationRequired):
        await Runner(rt).run(compiled, root, resume=True)
    assert calls == [1]


def test_retry_eligibility_schema_is_backward_compatible():
    from pathlib import Path
    import jsonschema
    from agent_playbook_os.models import InvocationRecord
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "invocation.schema.json").read_text())
    entry = InvocationRecord(step_id="step", attempt=1, kind="action")
    jsonschema.validate(entry.model_dump(mode="json"), schema)
    old = entry.model_dump(mode="json")
    old.pop("retry_eligible")
    assert InvocationRecord.model_validate(old).retry_eligible is False
    jsonschema.validate(old, schema)
