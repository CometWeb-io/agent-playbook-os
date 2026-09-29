from __future__ import annotations

import json
from pathlib import Path

from .errors import IntegrityError
from .models import RunAttestation
from .storage import open_run_store
from .signing import AttestationSigner, HMACSigner


def _canonical_payload(attestation: RunAttestation) -> bytes:
    data = attestation.model_dump(mode="json")
    data["signature"] = "pending"
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def create_attestation_with_signer(
    run_dir: str | Path,
    signer: AttestationSigner,
    *,
    artifact_verifier=None,
    tenant_scope=None,
) -> RunAttestation:
    store = open_run_store(run_dir)
    checks = [store.verify_event_chain(), store.verify_plan_integrity(), store.verify_state_integrity()]
    failures = [error for ok, error in checks if not ok]
    if failures:
        raise IntegrityError("cannot attest invalid run: " + "; ".join(x for x in failures if x))
    plan = store.load_plan()
    state = store.load_state()
    artifact_ok, artifact_errors = store.verify_artifacts(state.artifacts, artifact_verifier, tenant_scope)
    if not artifact_ok:
        raise IntegrityError("cannot attest invalid artifacts: " + "; ".join(artifact_errors))
    events = store.read_events()
    att = RunAttestation(
        run_id=state.run_id,
        plan_integrity_hash=plan.integrity_hash,
        plan_semantic_hash=plan.semantic_hash,
        state_integrity_hash=state.integrity_hash,
        event_head_hash=events[-1].get("event_hash") if events else None,
        artifact_hashes={x.id: x.content_hash for x in state.artifacts if x.content_hash},
        signer=signer.signer_id,
    )
    att.signature = signer.sign(_canonical_payload(att))
    return att


def verify_attestation_with_signer(
    run_dir: str | Path,
    attestation: RunAttestation,
    signer: AttestationSigner,
    *,
    artifact_verifier=None,
    tenant_scope=None,
) -> tuple[bool, str | None]:
    if attestation.signer != signer.signer_id:
        return False, f"signer mismatch: attestation={attestation.signer} verifier={signer.signer_id}"
    if not signer.verify(_canonical_payload(attestation), attestation.signature):
        return False, "attestation signature mismatch"
    try:
        current = create_attestation_with_signer(
            run_dir, signer, artifact_verifier=artifact_verifier, tenant_scope=tenant_scope
        )
    except Exception as exc:
        return False, str(exc)
    fields = [
        "run_id",
        "plan_integrity_hash",
        "plan_semantic_hash",
        "state_integrity_hash",
        "event_head_hash",
        "artifact_hashes",
    ]
    for field in fields:
        if getattr(current, field) != getattr(attestation, field):
            return False, f"attested {field} differs from current run"
    return True, None


def create_attestation(run_dir: str | Path, key: bytes, *, signer: str = "local-hmac", artifact_verifier=None, tenant_scope=None) -> RunAttestation:
    wrapped = HMACSigner(key)
    if signer != wrapped.signer_id:
        # Preserve the legacy signer label only for backwards-compatible local HMAC
        # use. Generic external signers must use create_attestation_with_signer.
        wrapped.signer_id = signer
    return create_attestation_with_signer(run_dir, wrapped, artifact_verifier=artifact_verifier, tenant_scope=tenant_scope)


def verify_attestation(
    run_dir: str | Path, attestation: RunAttestation, key: bytes, *, artifact_verifier=None, tenant_scope=None
) -> tuple[bool, str | None]:
    wrapped = HMACSigner(key)
    wrapped.signer_id = attestation.signer
    return verify_attestation_with_signer(
        run_dir, attestation, wrapped, artifact_verifier=artifact_verifier, tenant_scope=tenant_scope
    )
