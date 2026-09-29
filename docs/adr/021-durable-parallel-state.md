# ADR-021: Durable nested state for parallel branches

## Decision

Persist branch-step state under stable `parent/branch/step` identities.

## Rationale

Parallel work cannot support human approval/recovery safely if nested progress exists only in memory.

## Consequence

Nested approvals can pause a run without replaying completed siblings. Cross-branch dependencies remain unsupported.
