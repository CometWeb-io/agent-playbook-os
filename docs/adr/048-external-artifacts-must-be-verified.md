# ADR-048: External artifacts must be verified

Status: accepted

## Decision

Non-local artifact URIs require an explicit scheme verifier and tenant scope; silent skipping is forbidden.

## Consequence

Production adapters may use different infrastructure, but they must preserve the same observable safety contract and pass the relevant conformance tests.
