# ADR-046: Distributed claims use fencing

Status: accepted

## Decision

Queue claims and run-resource leases carry monotonically increasing fences; stale holders cannot acknowledge newer ownership.

## Consequence

Production adapters may use different infrastructure, but they must preserve the same observable safety contract and pass the relevant conformance tests.
