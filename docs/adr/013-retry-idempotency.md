# ADR-013: Retry requires explicit idempotency for material side effects

## Decision

Retries of `external` or `destructive` side effects require an explicit, stable `idempotency_key` unless policy is deliberately relaxed.

## Why

A retry is a second execution, not merely a second reasoning attempt. Repeating an email, charge, deployment, CRM mutation, or write can cause irreversible duplication. The kernel therefore rejects unsafe retry plans during compilation and preserves the rendered key across attempts.

Resume is separate: a failed side-effect step without an idempotency key must encounter its approval gate again before it can be re-executed.
