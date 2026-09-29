# ADR-045: Work submission is idempotent

Status: accepted

## Decision

Stable work IDs bind to one semantic payload and conflicting reuse fails closed.

## Consequence

Production adapters may use different infrastructure, but they must preserve the same observable safety contract and pass the relevant conformance tests.
