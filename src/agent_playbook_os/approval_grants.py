"""Host-issued, scope-bound HMAC grants for queued human approvals.

Grant signing is intended to happen in a trusted approval service AFTER the
human has been authenticated and authorized. Signing arbitrary caller-provided
actor names is not authentication. This module only authenticates the issued
artifact to a worker that possesses the same secret trust root.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_STEP_ID = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
MAX_TTL_SECONDS = 86400
MAX_GRANT_BYTES = 16384


class ApprovalGrantError(ValueError):
    """A queue approval request is unsigned, expired or does not match its scope."""


class SignedApprovalGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["agent-playbook-os/approval-grant/v1"] = (
        "agent-playbook-os/approval-grant/v1"
    )
    audience: str = Field(min_length=1, max_length=128)
    key_id: str = Field(min_length=1, max_length=128)
    issuer: str = Field(min_length=1, max_length=256)
    actor: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=64)
    namespace: str = Field(min_length=1, max_length=64)
    queue_name: str = Field(min_length=1, max_length=128)
    work_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    plan_integrity_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    approved_step_ids: list[str] = Field(min_length=1, max_length=128)
    not_before: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    nonce: str = Field(min_length=1, max_length=128)
    signature: str = Field(pattern=r"^hmac-sha256:[a-f0-9]{64}$")

    @field_validator("approved_step_ids")
    @classmethod
    def _validate_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate approval step ID")
        if any(not _STEP_ID.fullmatch(item) for item in value):
            raise ValueError("invalid approval step ID")
        return value

    @model_validator(mode="after")
    def _check_window(self):
        if self.expires_at <= self.not_before:
            raise ValueError("grant validity window must be positive")
        if self.expires_at - self.not_before > MAX_TTL_SECONDS:
            raise ValueError("grant validity exceeds 24 hours")
        return self


def _payload_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(
        {key: value for key, value in data.items() if key != "signature"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


class HMACApprovalAuthorizer:
    """Validate approval grants against a trusted keyring and queue submission.

    The issuer should authenticate and authorize the actor independently. A
    caller-controlled WorkSubmission can carry a signed grant, but cannot sign
    one. A grant binds one work ID, run ID, tenant, namespace, queue and exact
    compiled plan hash. It cannot be replayed for a DIFFERENT work ID; retrying
    the same claimed work is allowed for durable at-least-once recovery.
    """

    def __init__(
        self,
        keys: Mapping[str, bytes],
        *,
        audience: str,
        clock: Callable[[], int] | None = None,
        clock_skew_seconds: int = 30,
    ):
        if not isinstance(audience, str) or not audience or len(audience) > 128:
            raise ValueError("audience must be 1..128 characters")
        if type(clock_skew_seconds) is not int or not 0 <= clock_skew_seconds <= 300:
            raise ValueError("clock_skew_seconds must be in 0..300")
        if not keys:
            raise ValueError("approval keyring must not be empty")
        self.keys = {}
        for key_id, key in keys.items():
            if not isinstance(key_id, str) or not 0 < len(key_id) <= 128:
                raise ValueError("invalid approval key ID")
            if type(key) is not bytes or len(key) < 32:
                raise ValueError("approval HMAC keys must be at least 32 bytes")
            self.keys[key_id] = key
        self.audience = audience
        self.clock = clock or (lambda: int(time.time()))
        self.clock_skew_seconds = clock_skew_seconds

    def issue(
        self, submission: Any, plan: Any, *,
        approved_step_ids: set[str] | list[str],
        actor: str,
        issuer: str,
        key_id: str,
        ttl_seconds: int = 900,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """For trusted approval services only; do not expose as unauthenticated API."""
        if key_id not in self.keys:
            raise ApprovalGrantError("unknown approval key ID")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError("ttl_seconds must be within 1..86400")
        now = self.clock()
        data = {
            "audience": self.audience,
            "key_id": key_id,
            "actor": actor,
            "issuer": issuer,
            "tenant_id": submission.tenant_id,
            "namespace": submission.namespace,
            "queue_name": submission.queue_name,
            "work_id": submission.work_id,
            "run_id": submission.run_id,
            "plan_integrity_hash": plan.integrity_hash,
            "approved_step_ids": sorted(approved_step_ids),
            "not_before": now,
            "expires_at": now + ttl_seconds,
            "nonce": nonce or str(uuid4()),
        }
        unsigned = {**data, "signature": "hmac-sha256:" + "0" * 64}
        grant = SignedApprovalGrant.model_validate(unsigned)
        body = grant.model_dump(mode="json")
        mac = hmac.new(self.keys[key_id], _payload_bytes(body), hashlib.sha256).hexdigest()
        body["signature"] = "hmac-sha256:" + mac
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_GRANT_BYTES:
            raise ApprovalGrantError("approval grant too large")
        return body

    def __call__(self, item: Any, plan: Any) -> tuple[set[str], str]:
        raw = item.metadata.get("approval_grant")
        if not item.approvals:
            if raw is not None:
                raise ApprovalGrantError("unexpected approval grant for work without approvals")
            return set(), "worker"
        if not isinstance(raw, dict):
            raise ApprovalGrantError("queued approvals require a signed approval_grant")
        if len(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8")) > MAX_GRANT_BYTES:
            raise ApprovalGrantError("approval grant too large")
        try:
            grant = SignedApprovalGrant.model_validate(raw)
        except Exception as exc:
            raise ApprovalGrantError("invalid approval grant structure") from exc
        key = self.keys.get(grant.key_id)
        if key is None:
            raise ApprovalGrantError("unknown approval signing key")
        actual = hmac.new(key, _payload_bytes(raw), hashlib.sha256).hexdigest()
        expected = "hmac-sha256:" + actual
        if not hmac.compare_digest(expected, grant.signature):
            raise ApprovalGrantError("approval grant signature mismatch")
        now = self.clock()
        if now + self.clock_skew_seconds < grant.not_before:
            raise ApprovalGrantError("approval grant not yet valid")
        if now >= grant.expires_at:
            raise ApprovalGrantError("approval grant expired")
        expected_scope = {
            "audience": self.audience,
            "work_id": item.work_id,
            "run_id": item.run_id,
            "tenant_id": item.tenant_id,
            "namespace": item.namespace,
            "queue_name": item.queue_name,
            "plan_integrity_hash": plan.integrity_hash,
        }
        for name, expected_value in expected_scope.items():
            if getattr(grant, name) != expected_value:
                raise ApprovalGrantError(f"approval grant scope mismatch: {name}")
        if set(grant.approved_step_ids) != set(item.approvals):
            raise ApprovalGrantError("approved steps differ from submitted work requests")
        return set(grant.approved_step_ids), grant.actor
