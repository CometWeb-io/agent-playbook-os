class PlaybookError(Exception):
    """Base error for the control plane."""


class CompileError(PlaybookError):
    pass


class PolicyDenied(PlaybookError):
    pass


class ResolutionError(PlaybookError):
    pass


class StepExecutionError(PlaybookError):
    pass


class ExternalExecutionRequired(StepExecutionError):
    pass


class BudgetExceeded(StepExecutionError):
    pass


class IntegrityError(PlaybookError):
    pass


class CancellationRequested(StepExecutionError):
    pass


class ReconciliationRequired(StepExecutionError):
    pass


class LeaseConflict(StepExecutionError):
    pass


class SchemaVersionError(IntegrityError):
    pass


class ApprovalRequired(StepExecutionError):
    def __init__(self, approval_id: str):
        super().__init__(f"approval required: {approval_id}")
        self.approval_id = approval_id


class SideEffectOutcomeUnknown(StepExecutionError):
    pass

class SecretResolutionError(StepExecutionError):
    pass


class CapabilityNegotiationError(StepExecutionError):
    pass


class PlanApprovalRequired(PlaybookError):
    def __init__(self, plan_semantic_hash: str):
        super().__init__(f"operator-generated ephemeral plan requires approval: {plan_semantic_hash}")
        self.plan_semantic_hash = plan_semantic_hash
