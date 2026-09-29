# ADR-032: Streaming is a first-class runtime lifecycle

## Decision

Allow runtime calls to emit typed progress/usage/artifact/evidence/receipt events and require one terminal `result` event.

## Rationale

Long model/tool operations need cancellation, usage accounting and provider receipts before final output without inventing a second execution model.

## Consequence

Streams share the same timeout, lease, cancellation, redaction, record/replay and uncertain-side-effect semantics as non-streaming invocations.
