# ADR: One operator by default

Status: Accepted

## Decision

Use one primary control-plane operator. Spawn isolated workers only for explicit isolation, independence, parallelism, or context-budget reasons.

## Consequences

This constraint is architectural. Changes require a superseding ADR and migration notes.
