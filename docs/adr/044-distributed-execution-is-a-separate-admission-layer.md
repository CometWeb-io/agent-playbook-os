# ADR-044: Distributed execution is a separate admission layer

Status: accepted

## Decision

The queue decides who may attempt an immutable plan; Runner remains authoritative for workflow semantics and recovery.

## Consequence

Production adapters may use different infrastructure, but they must preserve the same observable safety contract and pass the relevant conformance tests.
