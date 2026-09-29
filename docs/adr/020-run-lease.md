# ADR-020: One execution lease per run

## Decision

A runner must acquire an exclusive lease before creating or resuming execution state.

## Rationale

Two concurrent resume processes can both observe the same incomplete step and duplicate material work.

## Consequence

A second runner receives `LeaseConflict`. Same-host stale PID leases can be recovered; unknown-host leases require explicit break.
