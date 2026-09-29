# ADR-024: Cooperative cancellation with material-side-effect uncertainty

## Decision

Cancellation is observed through a durable marker/token and interrupts active coroutine execution.

## Rationale

Cancellation must be visible, auditable and usable across processes.

## Consequence

Local/no-side-effect invocations become cancelled. In-flight external/destructive invocations become `UNKNOWN` and require reconciliation.
