from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class InputDefinition(StrictModel):
    type: Literal["string", "integer", "number", "boolean", "object", "array"] = "string"
    required: bool = False
    default: Any = None
    description: str | None = None
    sensitive: bool = False


class Metadata(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: str
    description: str
    tags: list[str] = Field(default_factory=list)


class SkillDependency(StrictModel):
    source: str
    path: str | None = None
    ref: str | None = None
    version: str | None = None
    optional: bool = False


class Dependencies(StrictModel):
    skills: dict[str, SkillDependency] = Field(default_factory=dict)


class BudgetSpec(StrictModel):
    """Hard execution ceilings. None means unbounded at this layer."""

    max_run_seconds: float | None = Field(default=None, gt=0)
    max_step_seconds: float | None = Field(default=None, gt=0)
    max_total_attempts: int | None = Field(default=None, ge=1)
    max_action_calls: int | None = Field(default=None, ge=0)
    max_skill_calls: int | None = Field(default=None, ge=0)
    max_agent_calls: int | None = Field(default=None, ge=0)
    max_eval_calls: int | None = Field(default=None, ge=0)
    max_model_calls: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)


class ExecutionConfig(StrictModel):
    control: Literal["deterministic", "adaptive"] = "deterministic"
    isolation: Literal["inline", "sandbox", "subagent", "auto"] = "auto"
    max_steps: int = Field(default=50, ge=1, le=1000)
    max_parallel: int = Field(default=4, ge=1, le=64)
    max_foreach_items: int = Field(default=100, ge=1, le=10000)
    max_loop_iterations: int = Field(default=10, ge=1, le=1000)
    max_attempts: int = Field(default=2, ge=1, le=20)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)


class PolicySpec(StrictModel):
    allowed_step_types: list[str] | None = None
    allowed_skills: list[str] | None = None
    allowed_actions: list[str] | None = None
    denied_skills: list[str] = Field(default_factory=list)
    denied_actions: list[str] = Field(default_factory=list)
    require_approval_for: list[Literal["local", "external", "destructive"]] = Field(
        default_factory=lambda: ["external", "destructive"]
    )
    require_idempotency_for_retry: list[Literal["local", "external", "destructive"]] = Field(
        default_factory=lambda: ["external", "destructive"]
    )
    max_steps: int | None = Field(default=None, ge=1)
    max_parallel: int | None = Field(default=None, ge=1)
    max_foreach_items: int | None = Field(default=None, ge=1)
    max_loop_iterations: int | None = Field(default=None, ge=1)
    require_resolved_skills: bool = False
    require_isolation_for: dict[Literal["local", "external", "destructive"], Literal["sandbox", "subagent"]] = Field(
        default_factory=dict
    )
    budget: BudgetSpec = Field(default_factory=BudgetSpec)


class RetrySpec(StrictModel):
    max_attempts: int = Field(default=1, ge=1, le=20)
    backoff_seconds: float = Field(default=0.0, ge=0, le=300)
    backoff_multiplier: float = Field(default=1.0, ge=1.0, le=10.0)
    max_backoff_seconds: float = Field(default=60.0, ge=0, le=3600)
    retry_on: list[str] = Field(default_factory=list)


class BranchCase(StrictModel):
    when: str
    value: Any


class StepSpec(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    type: Literal["skill", "action", "agent", "gate", "branch", "parallel", "foreach", "while", "playbook", "eval"]
    needs: list[str] = Field(default_factory=list)
    when: str | None = None
    on_fail: Literal["stop", "skip", "continue"] = "stop"
    uses: str | None = None
    action: str | None = None
    gate: Literal["human", "condition", "policy", "evidence", "budget", "review"] | None = None
    condition: str | None = None
    playbook: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    cases: list[BranchCase] = Field(default_factory=list)
    default: Any = None
    branches: dict[str, list["StepSpec"]] = Field(default_factory=dict)
    items: Any = None
    item_var: str = Field(default="item", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    foreach_steps: list["StepSpec"] = Field(default_factory=list, alias="steps")
    loop_steps: list["StepSpec"] = Field(default_factory=list, alias="do")
    max_concurrency: int | None = Field(default=None, ge=1, le=64)
    max_iterations: int | None = Field(default=None, ge=1, le=1000)
    side_effects: Literal["none", "local", "external", "destructive"] = "none"
    requires_approval: bool = False
    execution_isolation: Literal["inline", "sandbox", "subagent", "auto"] | None = None
    output_schema: str | None = None
    output_schema_hash: str | None = Field(default=None, pattern=r"^(sha256:)?[a-fA-F0-9]{64}$")
    retry: RetrySpec = Field(default_factory=RetrySpec)
    timeout_seconds: float | None = Field(default=None, gt=0, le=86400)
    idempotency_key: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self):
        if self.type == "skill" and not self.uses:
            raise ValueError("skill step requires 'uses'")
        if self.type == "action" and not self.action:
            raise ValueError("action step requires 'action'")
        if self.type == "gate" and not self.gate:
            raise ValueError("gate step requires 'gate'")
        if self.type == "branch" and not self.cases:
            raise ValueError("branch step requires at least one case")
        if self.type == "parallel" and not self.branches:
            raise ValueError("parallel step requires branches")
        if self.type == "foreach":
            if self.items is None:
                raise ValueError("foreach step requires items")
            if not self.foreach_steps:
                raise ValueError("foreach step requires steps")
        if self.type == "while":
            if not self.condition:
                raise ValueError("while step requires condition")
            if not self.loop_steps:
                raise ValueError("while step requires do")
            if self.max_iterations is None:
                raise ValueError("while step requires max_iterations")
        if self.type == "playbook" and not self.playbook:
            raise ValueError("playbook step requires 'playbook'")
        return self


StepSpec.model_rebuild()


class PlaybookSpecBody(StrictModel):
    inputs: dict[str, InputDefinition] = Field(default_factory=dict)
    dependencies: Dependencies = Field(default_factory=Dependencies)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    policy: PolicySpec = Field(default_factory=PolicySpec)
    steps: list[StepSpec]
    outputs: dict[str, Any] = Field(default_factory=dict)


class Playbook(StrictModel):
    apiVersion: Literal["playbook.agent/v1alpha1"]
    kind: Literal["Playbook"]
    metadata: Metadata
    spec: PlaybookSpecBody


class SkillDescriptor(StrictModel):
    id: str
    version: str | None = None
    description: str | None = None
    path: str | None = None
    source: str | None = None
    content_hash: str | None = None
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    compatible_hosts: list[str] = Field(default_factory=list)


class SkillLockEntry(StrictModel):
    id: str
    source: str
    version: str | None = None
    ref: str | None = None
    path: str | None = None
    content_hash: str | None = None
    resolved: bool = False


class ArtifactRef(StrictModel):
    id: str
    kind: str = "artifact"
    uri: str
    media_type: str | None = None
    content_hash: str | None = None
    produced_by: str | None = None


class EvidenceRef(StrictModel):
    id: str
    uri: str
    schema_ref: str | None = None
    content_hash: str | None = None
    observed_at: str | None = None


class UsageMetrics(StrictModel):
    total_attempts: int = Field(default=0, ge=0)
    action_calls: int = Field(default=0, ge=0)
    skill_calls: int = Field(default=0, ge=0)
    agent_calls: int = Field(default=0, ge=0)
    eval_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    wall_time_seconds: float = Field(default=0.0, ge=0)

    def add(self, other: "UsageMetrics") -> "UsageMetrics":
        for field in type(self).model_fields:
            setattr(self, field, getattr(self, field) + getattr(other, field))
        return self


class ProviderReceipt(StrictModel):
    """Provider acknowledgement persisted separately from the model/tool payload.

    A receipt is evidence that a provider assigned an identity to an invocation. It is
    not, by itself, proof that a requested side effect reached the user's target state.
    Adapters should set ``status=completed`` only when the provider's contract makes
    that statement defensible.
    """

    receipt_id: str = Field(default_factory=lambda: str(uuid4()))
    provider: str
    call_id: str
    operation: str | None = None
    status: Literal["accepted", "completed", "unknown", "failed"] = "accepted"
    idempotency_key: str | None = None
    received_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    payload_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeStreamEvent(StrictModel):
    """Provider-neutral event emitted by a streaming runtime."""

    type: Literal["progress", "usage", "artifact", "evidence", "receipt", "result", "log"]
    value: Any = None
    usage: UsageMetrics | None = None
    artifact: ArtifactRef | None = None
    evidence_ref: EvidenceRef | None = None
    receipt: ProviderReceipt | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeResult(StrictModel):
    """Optional typed adapter result. Plain values remain supported for compatibility."""

    value: Any = None
    usage: UsageMetrics = Field(default_factory=UsageMetrics)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    provider_receipts: list[ProviderReceipt] = Field(default_factory=list)


class CompiledStep(StrictModel):
    spec: StepSpec
    ordinal: int


class CompiledPlan(StrictModel):
    schema_version: Literal["agent-playbook-os/compiled-plan/v2"] = "agent-playbook-os/compiled-plan/v2"
    playbook_id: str
    playbook_version: str
    playbook_hash: str
    semantic_hash: str
    integrity_hash: str
    compiled_at: str
    input_values: dict[str, Any]
    execution: ExecutionConfig
    policy: PolicySpec
    playbook_policy_hash: str | None = None
    host_policy_hash: str | None = None
    effective_policy_hash: str | None = None
    steps: list[CompiledStep]
    outputs: dict[str, Any]
    skill_lock: dict[str, SkillLockEntry] = Field(default_factory=dict)
    source_path: str | None = None


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class StepState(StrictModel):
    id: str
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None
    output: Any = None
    error: str | None = None
    approval_required: bool = False
    idempotency_key: str | None = None
    usage: UsageMetrics = Field(default_factory=UsageMetrics)
    invocation_ids: list[str] = Field(default_factory=list)
    provider_receipt_ids: list[str] = Field(default_factory=list)


class InvocationStatus(str, Enum):
    STARTED = "STARTED"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECONCILED = "RECONCILED"


class InvocationRecord(StrictModel):
    invocation_id: str = Field(default_factory=lambda: str(uuid4()))
    step_id: str
    attempt: int = Field(ge=1)
    kind: Literal["skill", "action", "agent", "eval"]
    side_effects: Literal["none", "local", "external", "destructive"] = "none"
    idempotency_key: str | None = None
    status: InvocationStatus = InvocationStatus.STARTED
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    result_hash: str | None = None
    error: str | None = None
    reconciled_by: str | None = None
    reconciliation_note: str | None = None
    provider_receipt_ids: list[str] = Field(default_factory=list)
    provider_call_id: str | None = None
    provider: str | None = None


class ApprovalRecord(StrictModel):
    step_id: str
    approved_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    actor: str = "human"
    reason: str | None = None

class RunState(StrictModel):
    schema_version: Literal["agent-playbook-os/run-state/v5"] = "agent-playbook-os/run-state/v5"
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    integrity_hash: str = "pending"
    plan_hash: str
    plan_semantic_hash: str
    playbook_hash: str
    playbook_id: str
    playbook_version: str
    status: RunStatus = RunStatus.CREATED
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    steps: dict[str, StepState] = Field(default_factory=dict)
    nested_steps: dict[str, StepState] = Field(default_factory=dict)
    invocations: list[InvocationRecord] = Field(default_factory=list)
    provider_receipts: list[ProviderReceipt] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    pending_approvals: list[str] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    usage: UsageMetrics = Field(default_factory=UsageMetrics)
    replayed_from: str | None = None
    parent_run_id: str | None = None
    root_run_id: str | None = None
    lineage_depth: int = Field(default=0, ge=0)
    fork_reason: str | None = None
    cancellation_reason: str | None = None
    cancelled_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    runtime_capabilities_hash: str | None = None
    storage_backend: str = "filesystem"


class PlaybookLock(StrictModel):
    schema_version: Literal["agent-playbook-os/lock/v2"] = "agent-playbook-os/lock/v2"
    playbook_id: str
    playbook_version: str
    playbook_hash: str
    plan_integrity_hash: str | None = None
    plan_semantic_hash: str | None = None
    skills: dict[str, SkillLockEntry] = Field(default_factory=dict)


class RunAttestation(StrictModel):
    schema_version: Literal["agent-playbook-os/attestation/v1"] = "agent-playbook-os/attestation/v1"
    run_id: str
    plan_integrity_hash: str
    plan_semantic_hash: str
    state_integrity_hash: str
    event_head_hash: str | None = None
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    attested_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    signer: str = "local-hmac"
    signature: str = "pending"


class Event(StrictModel):
    schema_version: Literal["agent-playbook-os/event/v1"] = "agent-playbook-os/event/v1"
    seq: int
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: str
    type: str
    step_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str | None = None
    event_hash: str | None = None
