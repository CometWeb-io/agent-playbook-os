# ADR-033: Capability negotiation is explicit admission control

## Decision

Compile plan requirements separately from runtime capabilities and support strict preflight before execution side effects.

## Rationale

Discovering that an adapter lacks a required action/isolation boundary after a workflow starts produces partial work and unsafe fallback pressure.

## Consequence

`preflight` can reject mismatches early. Capability declarations remain claims, not security certification or permission grants.
