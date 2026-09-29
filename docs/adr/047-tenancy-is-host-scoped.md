# ADR-047: Tenancy is host scoped

Status: accepted

## Decision

Tenant and namespace identity come from the host/queue boundary, not untrusted playbook content; policy composition is monotonic.

## Consequence

Production adapters may use different infrastructure, but they must preserve the same observable safety contract and pass the relevant conformance tests.
