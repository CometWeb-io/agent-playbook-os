# Playbook specification v1alpha1

Canonical API version: `playbook.agent/v1alpha1`.

The specification is language-neutral. The Python package is the reference implementation.

## Document

```yaml
apiVersion: playbook.agent/v1alpha1
kind: Playbook
metadata:
  id: example
  version: 0.6.0
  description: Human-readable purpose.
spec:
  inputs: {}
  dependencies: {skills: {}}
  execution: {}
  policy: {}
  steps: []
  outputs: {}
```

## Inputs and secrets

Inputs can be marked `sensitive: true`. The reference compiler rejects raw sensitive literals. A sensitive value must be passed by reference, for example `env://TOKEN`, `secret://crm/api-key`, or `file-secret://...`, so the host owns secret resolution rather than the persisted playbook state.

## Data references

Templates use `{{ ... }}`. An exact template returns the native value; embedded templates interpolate text.

Available roots:

- `inputs`
- `steps`
- `run`
- `control` during an active attempt

Conditions use the same roots with a restricted AST grammar. Function calls, imports, arbitrary Python execution, and oversized expressions are rejected.

Step IDs may contain hyphens. `steps.my-step.output` is normalized safely by the reference evaluator.

A step referencing another top-level step must declare it in `needs`. Nested parallel steps declare local dependencies themselves; external dependencies must also be declared by the parent parallel step. Cross-branch dependencies remain invalid because they create ambiguous scheduling and recovery ownership.

## Compilation identities

A compiled plan contains:

- `playbook_hash` — canonical source playbook content;
- `semantic_hash` — stable compiled semantics excluding timestamp and host-local resolution paths;
- `integrity_hash` — exact compiled artifact integrity.

Runtime persists semantic and integrity identities separately. The semantic hash is suitable for reproducibility/diffing; the integrity hash is for exact tamper detection.

## Execution config

Key controls:

- `control`: `deterministic | adaptive`;
- `isolation`: `inline | sandbox | subagent | auto`;
- `max_steps`;
- `max_parallel`;
- `max_attempts` — hard ceiling for any step retry specification;
- `budget` — wall time, per-step time, call, token and cost ceilings.

Isolation is a contract, not a hint. A step requiring `sandbox` or `subagent` fails closed if the selected runtime cannot prove that capability.

## Steps

### skill

Required: `uses`. The skill must appear under `spec.dependencies.skills`.

### action

Required: `action`. Reference built-ins are `set`, `echo`, `sleep`, and opt-in `command`.

### agent

A bounded reasoning task executed by a host adapter. It is not assumed to be autonomous or persistent.

### gate

Required: `gate`. `human` pauses unless approved. `budget` re-checks effective limits. Other gate classes may use `condition`.

### branch

Ordered `cases`, each with `when` and `value`, plus optional `default`. Selected value is emitted at `output.selected`.

### parallel

`branches` maps branch names to nested step lists. Branches execute concurrently; steps inside a branch may have local dependencies.

v0.4 persists nested branch step state independently. Human gates and policy-required approvals inside a parallel branch can pause and resume safely using stable nested identities such as `parallel-step/branch/child-step`. Completed sibling work is not re-executed after resume.

The current v1alpha1 reference kernel still rejects:

- cross-branch dependencies;
- nested `parallel` inside another parallel branch.

### playbook

Required: relative `playbook` path. Paths are confined to the parent playbook directory after symlink resolution.

### eval

Delegates to the eval runtime. Reference runtime supports exact `actual == expected` assertions.

## Conditions and `when`

`when` is evaluated before approval and execution. False conditions mark a step `SKIPPED`.

## Failure policy

Each step supports:

- `stop` — default; fail the run;
- `skip` — mark the step skipped and continue;
- `continue` — keep the step `FAILED` and let downstream steps inspect its error/output state.

Integrity, policy, reconciliation and budget failures fail closed; `skip`/`continue` do not override them.

## Retries and invocation identity

```yaml
retry:
  max_attempts: 3
  backoff_seconds: 1
  backoff_multiplier: 2
  max_backoff_seconds: 30
  retry_on: [TimeoutError, ConnectionError]
```

An empty `retry_on` retries ordinary execution failures up to the attempt limit. Budget and integrity failures are never retried.

For side-effect classes configured in `policy.require_idempotency_for_retry`, more than one attempt requires `idempotency_key`. The rendered idempotency key stays stable across attempts and is exposed through `control.idempotency_key`.

Every runtime invocation receives a durable invocation record. For `external` or `destructive` work, a timeout/cancellation/crash can yield `UNKNOWN` rather than pretending the operation failed. Unknown material outcomes are not retried automatically unless the adapter explicitly proves idempotent execution. Otherwise the run requires reconciliation before resume.

## Timeouts and cancellation

`timeout_seconds` is a per-attempt ceiling. Effective timeout is the minimum of:

- step timeout;
- execution `budget.max_step_seconds`;
- policy `budget.max_step_seconds`;
- remaining run wall-time budget.

Cancellation is cooperative and durable. A cancel marker is persisted outside the model context, the active invocation is interrupted, and run state becomes `CANCELLED`. Material side effects that were in flight can remain `UNKNOWN` and require reconciliation.

## Output schemas

Local JSON schema paths are validated after execution and confined to the playbook directory. A local schema may optionally be pinned with `output_schema_hash`.

Remote HTTP(S) schemas are accepted only when:

1. the playbook includes a SHA-256 `output_schema_hash`, and
2. the host provides an explicit schema resolver.

The resolver bytes must match the declared pin before validation. Unpinned or unresolved remote schemas fail closed.

## Side effects and approvals

`side_effects` is one of:

- `none`
- `local`
- `external`
- `destructive`

Policy can force explicit approval for any class and can require a minimum isolation boundary for material classes. Approval records include step identity, actor and timestamp, are persisted in run state, and remain authoritative after resume for the same invocation boundary.

## RuntimeResult

Adapters may return a plain value or typed `RuntimeResult` with domain `value`, usage metrics, artifact refs and evidence refs.

## Durable state compatibility

`RunState` is a compatibility boundary. v0.6 continues to write `agent-playbook-os/run-state/v5` and migrates supported v2/v3/v4 snapshots through an explicit registry. Unsupported future schema versions fail explicitly instead of being guessed.

## Compatibility

The v1alpha1 surface may evolve before 1.0. Semantic migration notes must accompany breaking schema changes.


## Foreach

`foreach` evaluates `items` once, requires an array, and creates durable nested identities per item. `execution.max_foreach_items` is a hard ceiling and `max_concurrency` cannot exceed the execution parallelism ceiling. Item bodies may pause for approval and resume without replaying completed nested work.

Runtime context exposes the configured `item_var` and `foreach_index`.

## While refinement loops

`while` is deliberately bounded:

```yaml
- id: refine
  type: while
  condition: loop.iteration < 3
  max_iterations: 3
  do:
    - id: review
      type: eval
      with:
        candidate: "{{ loop.previous }}"
```

`max_iterations` is mandatory and cannot exceed `execution.max_loop_iterations` or the host policy ceiling. Each iteration and nested step has a stable durable identity such as `refine/iteration-0001/review`. Context exposes `loop.iteration`, `loop.previous`, and `loop.history`.

If the condition is still true after the configured maximum, the loop fails closed instead of silently accepting an unfinished refinement cycle. Nested `parallel`, `foreach`, and `while` inside a while body remain intentionally unsupported in v1alpha1.
