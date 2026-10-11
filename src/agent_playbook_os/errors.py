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


class ProviderEvidenceConflict(ReconciliationRequired):
    """Contradictory provider receipts share the same immutable evidence ID.

    The downstream operation may already have occurred. Do not retry or
    convert this into success until authoritative reconciliation.
    """


class LeaseConflict(StepExecutionError):
    pass


class SchemaVersionError(IntegrityError):
    pass


class ApprovalRequired(StepExecutionError):
    def __init__(self, approval_id: str):
        super().__init__(f"approval required: {approval_id}")
        self.approval_id = approval_id


class SideEffectOutcomeUnknown(StepExecutionError):
    """The material operation may have taken effect; its outcome is unknown."""


class NoSideEffectPerformed(StepExecutionError):
    """Host adapter attests that the requested operation never took effect.

    Use only after checking the provider's authoritative response or before
    dispatch. An ordinary exception, network timeout or HTTP error cannot
    establish that an external side effect was absent.
    """


class SecretResolutionError(StepExecutionError):
    pass


class CapabilityNegotiationError(StepExecutionError):
    pass


class PlanApprovalRequired(PlaybookError):
    def __init__(self, plan_semantic_hash: str):
        super().__init__(f"operator-generated ephemeral plan requires approval: {plan_semantic_hash}")
        self.plan_semantic_hash = plan_semantic_hash


class ProviderContinuationRequired(StepExecutionError):
    """A provider returned a nonterminal response or an unhandled tool request.

    The reference adapter cannot safely perform provider-native continuation.
    Hosts should reconcile/resume using the provider's response identity.
    """


class StreamProtocolError(ProviderContinuationRequired):
    """Provider stream exceeded host safety limits or broke its terminal contract.

    A material invocation interrupted by this error has an unknown downstream
    outcome and must be reconciled before any retry.
    """


class ProviderRefusal(StepExecutionError):
    """The model explicitly refused the requested operation."""
