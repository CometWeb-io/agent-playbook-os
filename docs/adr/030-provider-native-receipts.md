# ADR-030: Preserve provider-native acknowledgement identity

## Decision

Runtime results/streams may emit typed `ProviderReceipt` records linked to durable invocation and step identities.

## Rationale

Local invocation IDs are insufficient for reconciling with provider logs or systems of record after uncertain outcomes.

## Consequence

Adapters should preserve real request/message/job IDs when available. A receipt improves provenance but does not automatically prove business success or complete a step.
