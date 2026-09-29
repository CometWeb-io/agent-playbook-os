# ADR-035: Plugin discovery is metadata, not authorization

## Decision

Discover runtime/store/telemetry/secret/schema-resolver plugins through entry-point descriptors and load provider code only after explicit host selection.

## Rationale

Automatic import/execution during discovery would turn package installation order into an authorization boundary and increase supply-chain risk.

## Consequence

Duplicate plugin names are ambiguity errors. Playbook content cannot silently install or select arbitrary execution plugins.
