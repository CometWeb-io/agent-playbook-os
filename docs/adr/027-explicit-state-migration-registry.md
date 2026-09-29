# ADR-027: Persisted state evolves through an explicit migration registry

## Decision

Treat each released `RunState` schema as a compatibility boundary and migrate only through registered version-to-version functions.

## Rationale

Ad-hoc default injection hides semantic changes and makes resume correctness impossible to reason about across releases.

## Consequence

v0.4 writes v4, reads supported v2/v3 through the registry, rejects unknown future versions, and exposes explicit persisted migration with backup.
