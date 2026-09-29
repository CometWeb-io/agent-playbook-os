# ADR-026: Adapter conformance is a first-class contract

## Decision

Expose provider-neutral runtime conformance checks and a callable adapter bridge.

## Rationale

Provider integrations should be tested against kernel obligations instead of relying on documentation or nominal method compatibility.

## Consequence

New adapters can reuse the same contract checks and add provider-specific assertions for permissions, idempotency and isolation.
