# ADR-028: Execution depends on a durable store protocol

## Decision

Runner code targets a store contract rather than filesystem paths. Filesystem and SQLite are reference implementations.

## Rationale

Persistence mechanics should be replaceable without changing workflow semantics or provider adapters.

## Consequence

Backends must preserve plan/state/event/cancellation/lease/reconciliation invariants. Backend-specific guarantees remain explicit; SQLite is not presented as a distributed database.
