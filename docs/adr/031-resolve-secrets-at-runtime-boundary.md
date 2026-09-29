# ADR-031: Playbooks persist secret references, not resolved values

## Decision

Resolve `env://`, `secret://` and `file-secret://` references immediately before runtime invocation through a pluggable resolver boundary.

## Rationale

Compilation, state snapshots, events, cassettes and traces should not become credential stores.

## Consequence

Resolved values use a secret marker and are redacted before durable kernel persistence. Host adapters remain responsible for not leaking credentials after receiving them.
