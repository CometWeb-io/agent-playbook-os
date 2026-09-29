# Evals, failure injection and recovery

Agent Playbook OS treats recovery behavior as part of correctness.

## Test layers

- unit tests for compiler, expressions, policy and stores;
- golden JSONL behavior suites;
- adversarial/security cases;
- deterministic fault injection;
- record/replay cassette tests;
- runtime conformance checks;
- benchmark runs;
- release-level smoke and repository validation.

## Required recovery cases

A serious adapter/playbook should prove:

1. completed steps are not repeated after resume;
2. approval decisions retain stable identity;
3. external/destructive timeout does not become an ordinary retryable failure;
4. unknown outcomes block resume without reconciliation/idempotency proof;
5. cancellation reaches a terminal state promptly;
6. concurrent resume is rejected by a lease;
7. nested parallel progress survives pause/resume;
8. tampered plan/state/event/artifact evidence is rejected;
9. telemetry/export failures do not change workflow success;
10. runtime replay diverges loudly when invocation signatures change;
11. generated ephemeral plans do not execute before plan approval;
12. foreach/while nested progress survives approval and resume;
13. while loops fail closed when their iteration ceiling is exhausted.

## Golden suite

```bash
playbook eval evals/cases/kernel.jsonl
```

## Conformance

```bash
playbook conformance --dry-run
```

Provider adapters should add their own conformance fixtures rather than weakening the generic contract.
