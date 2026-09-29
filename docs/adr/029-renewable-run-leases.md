# ADR-029: Active run ownership uses renewable TTL leases

## Decision

Run ownership includes TTL plus heartbeat renewal rather than relying only on process identity.

## Rationale

PID checks do not work across hosts and stale ownership must eventually become recoverable without silently allowing simultaneous execution.

## Consequence

Lease loss interrupts local waiting. If a material operation may already have been dispatched, its invocation becomes `UNKNOWN` and requires safe recovery.
