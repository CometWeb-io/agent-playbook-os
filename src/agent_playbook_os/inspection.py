from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import InvocationStatus
from .storage import open_run_store


def inspect_run(run_dir: str | Path, *, artifact_verifier=None, tenant_scope=None) -> dict[str, Any]:
    store = open_run_store(run_dir)
    state = store.load_state()
    plan = store.load_plan()
    event_ok, event_error = store.verify_event_chain()
    plan_ok, plan_error = store.verify_plan_integrity()
    state_ok, state_error = store.verify_state_integrity()
    artifact_ok, artifact_errors = store.verify_artifacts(state.artifacts, artifact_verifier, tenant_scope)
    uncertain = [
        x.model_dump(mode="json") for x in state.invocations
        if x.status in {InvocationStatus.STARTED, InvocationStatus.UNKNOWN}
    ]
    return {
        "run_id": state.run_id,
        "status": state.status.value,
        "playbook": {"id": plan.playbook_id, "version": plan.playbook_version, "semantic_hash": plan.semantic_hash},
        "storage_backend": state.storage_backend,
        "state_schema": state.schema_version,
        "source_state_schema": store.source_state_version(),
        "pending_approvals": list(state.pending_approvals),
        "uncertain_invocations": uncertain,
        "provider_receipts": [x.model_dump(mode="json") for x in state.provider_receipts],
        "usage": state.usage.model_dump(mode="json"),
        "lease": store.lease_status(),
        "integrity": {
            "events": {"ok": event_ok, "error": event_error},
            "plan": {"ok": plan_ok, "error": plan_error},
            "state": {"ok": state_ok, "error": state_error},
            "artifacts": {"ok": artifact_ok, "errors": artifact_errors},
        },
        "event_count": len(store.read_events()),
        "step_count": len(state.steps),
        "nested_step_count": len(state.nested_steps),
        "invocation_count": len(state.invocations),
    }
