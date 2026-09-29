# ADR: Compile before execute

Status: Accepted

## Decision

Execution consumes an immutable compiled plan. Replanning produces a new auditable plan rather than mutating a live run invisibly.

## Consequences

This constraint is architectural. Changes require a superseding ADR and migration notes.
