from pathlib import Path
import json

from agent_playbook_os.evals import EvalCase, EvalSuiteResult
from agent_playbook_os.models import (
    ApprovalRecord,
    InvocationRecord,
    RunAttestation,
    ArtifactRef,
    BudgetSpec,
    CompiledPlan,
    Event,
    EvidenceRef,
    Playbook,
    PlaybookLock,
    PolicySpec,
    RunState,
    RuntimeResult,
    RuntimeStreamEvent,
    ProviderReceipt,
    UsageMetrics,
)
from agent_playbook_os.negotiation import NegotiationReport
from agent_playbook_os.planning import PlannerRequest, PlannerResponse, OperatorDecision, PlanGateReport
from agent_playbook_os.promotion import PromotionObservation, PromotionSummary, PromotionReceipt
from agent_playbook_os.lab import ReplayLabReport
from agent_playbook_os.distributed import WorkSubmission, WorkItem, QueueStats, TenantScope, FencingLease

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "schemas"
OUT.mkdir(exist_ok=True)
models = {
    "playbook.schema.json": Playbook,
    "compiled-plan.schema.json": CompiledPlan,
    "run-state.schema.json": RunState,
    "event.schema.json": Event,
    "policy.schema.json": PolicySpec,
    "budget.schema.json": BudgetSpec,
    "artifact.schema.json": ArtifactRef,
    "evidence-ref.schema.json": EvidenceRef,
    "lock.schema.json": PlaybookLock,
    "usage.schema.json": UsageMetrics,
    "runtime-result.schema.json": RuntimeResult,
    "runtime-stream-event.schema.json": RuntimeStreamEvent,
    "provider-receipt.schema.json": ProviderReceipt,
    "negotiation-report.schema.json": NegotiationReport,
    "approval.schema.json": ApprovalRecord,
    "invocation.schema.json": InvocationRecord,
    "attestation.schema.json": RunAttestation,
    "eval-case.schema.json": EvalCase,
    "eval-suite-result.schema.json": EvalSuiteResult,
    "planner-response.schema.json": PlannerResponse,
    "planner-request.schema.json": PlannerRequest,
    "operator-decision.schema.json": OperatorDecision,
    "plan-gate.schema.json": PlanGateReport,
    "promotion-observation.schema.json": PromotionObservation,
    "promotion-summary.schema.json": PromotionSummary,
    "promotion-receipt.schema.json": PromotionReceipt,
    "replay-lab-report.schema.json": ReplayLabReport,
    "work-submission.schema.json": WorkSubmission,
    "work-item.schema.json": WorkItem,
    "queue-stats.schema.json": QueueStats,
    "tenant-scope.schema.json": TenantScope,
    "fencing-lease.schema.json": FencingLease,
}
for name, model in models.items():
    (OUT / name).write_text(json.dumps(model.model_json_schema(), indent=2), encoding="utf-8")
    print(name)
