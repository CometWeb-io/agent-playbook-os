# ADR-050: Local-write uncertainty requires reconciliation

## Decision

Supersedes the local-side-effect consequence of ADR-024. Local writes are
material operations, like external/destructive writes, for interrupted
invocation recovery. They are not equivalent to no-side-effect work.

## Rationale

A process may write files before timing out or being cancelled. Marking that
invocation failed/cancelled does not prove the files are unchanged and can
allow an unsafe retry. A crash can leave the same uncertainty as `STARTED`.
An adapter can also report `SideEffectOutcomeUnknown` despite an incorrect
`side_effects: none` declaration; the uncertainty evidence takes precedence.

## Consequences

- Timeout, cancellation and lease loss during local/external/destructive work
  persist `UNKNOWN` through top-level, parallel, foreach and while paths.
- Resume reconciles every `UNKNOWN` and every material `STARTED` record before
  dispatch. Existing adapter-proven downstream idempotency remains an explicit
  exception; a prompt key is not proof of idempotency.
- Retrying a container cannot bypass reconciliation of its interrupted
  children. A parent's default no-effect declaration is not a summary of its
  descendants. Output-limit interruption after command dispatch also retains
  material-outcome uncertainty.
- No-side-effect ordinary cancellation remains `CANCELLED`.
- Completed work is preserved. Interrupted local workflows can require manual
  reconciliation more often, including when no file ultimately changed.
- Existing invocation states and durable-store contracts are reused. No
  persisted schema migration or provider-specific kernel behavior is added.
