# ADR-049: SQLite defines semantics, not scale

Status: accepted

## Decision

SQLite queue/fencing implementations are executable references for adapter conformance, not production multi-region infrastructure.

## Consequence

Production adapters may use different infrastructure, but they must preserve the same observable safety contract and pass the relevant conformance tests.
