# ADR-023: Remote schemas require content pins

## Decision

A remote `output_schema` must include a SHA-256 pin and an explicit host resolver.

## Rationale

Implicit network fetch makes validation mutable and creates a supply-chain boundary hidden from the compiled plan.

## Consequence

Unpinned remote schemas fail compilation. The reference runtime never fetches them implicitly.
