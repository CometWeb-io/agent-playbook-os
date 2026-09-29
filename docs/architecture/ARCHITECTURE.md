# Architecture

## Thesis

Agent Playbook OS is a **control plane**, not a society of role-playing agents. It coordinates one primary operator, deterministic workflow control, external skill libraries, tools, approvals, budgets and optional isolated reasoning workers.

The kernel enforces:

```text
policy > compiled playbook > operator > skill > action/tool
```

## Control flow

```text
goal
  -> operator selection/proposal
  -> playbook validation
  -> compiler
  -> immutable compiled plan
  -> capability negotiation / policy preflight
  -> renewable runner lease
  -> runner
      -> skill/action/agent/gate/subplaybook
      -> invocation journal + provider receipts
      -> secret resolution at runtime boundary
      -> state checkpoint through RunStore protocol
      -> event append
      -> budget/policy/isolation checks
      -> pause/cancel/reconcile when required
  -> outputs + provenance
  -> eval/replay/diff/attestation
```

## Operator

Chooses a reusable playbook or, in an adaptive host, may propose an ephemeral one. Any proposal remains data until it passes normal validation, compilation, host-policy composition, dependency checks, capability preflight and an explicit plan-level approval for newly generated workflows.

The operator does not own permissions and cannot invisibly mutate an active compiled plan. Exact Operator decision/gate/compiled artifacts are persisted for review; a reviewed compiled plan can execute without re-planning.

## Compiler

Compilation performs structural and safety checks before execution:

1. validate inputs and types;
2. reject raw sensitive-input values;
3. inventory top-level and nested steps;
4. enforce globally unique step IDs;
5. validate explicit dependencies and data references;
6. reject dependency/cross-branch cycles and unsupported nested synchronization;
7. validate retry/idempotency rules and attempt ceilings;
8. require hash pins for remote output schemas;
9. resolve and content-lock skills when configured;
10. run policy preflight;
11. compute source, semantic and exact-integrity hashes.

Runtime executes the compiled plan, not mutable source YAML.

## Identities

Three hashes serve different purposes:

- `playbook_hash`: source content identity;
- `semantic_hash`: portable executable semantics;
- `integrity_hash`: exact compiled artifact identity.

This prevents local timestamps/paths from destroying reproducibility while still detecting tampering of a persisted plan.

## Runner

The runner owns transition semantics:

- per-run lease;
- atomic state snapshots;
- hash-chained events;
- approvals;
- invocation identity and uncertain-outcome tracking;
- retries/backoff and idempotency context;
- step/run timeouts;
- usage and budget accounting;
- cooperative cancellation;
- pause/resume and failed-run recovery;
- durable parallel branch execution;
- runtime result aggregation;
- pinned schema validation;
- nested playbooks.

Unsupported safety behavior fails closed.

## Invocation boundary

A runtime invocation is not synonymous with a step. One step can have multiple attempts, and each attempt receives a durable invocation identity.

For material side effects the journal distinguishes `FAILED` from `UNKNOWN`. Timeout or cancellation after dispatch may make the outcome unknown even if the local coroutine ended. Resume is blocked until the uncertainty is resolved or an adapter proves idempotent retry.

## Runtime adapters

The kernel defines an `ExecutionRuntime` boundary rather than importing a provider SDK. Runtime adapters implement skill, action/tool, bounded agent and eval execution.

Adapters may return a plain value, typed `RuntimeResult`, or an async stream of typed runtime events. Results/streams may carry usage, artifacts, evidence references and provider-native receipts. Adapters should expose capability truth about actions, isolation, idempotency and provider features.

`CallableRuntime` is the generic host bridge. Injected-client OpenAI Responses and Anthropic Messages adapters plus a Cursor host bridge demonstrate provider integration without vendor SDK imports in core.

## Isolation

Isolation is orthogonal to step type. A skill or agent may run inline, in a sandbox or as an isolated subagent depending on host capability and policy.

A declared boundary is never trusted by itself. If policy or step configuration requires `sandbox`/`subagent`, the runtime must explicitly support it or execution fails closed.

## Step types

- `skill` — versioned specialist procedure;
- `action` — deterministic tool/action call;
- `agent` — bounded open-ended reasoning task;
- `gate` — condition, policy, evidence, budget, review or human approval;
- `branch` — deterministic selection;
- `parallel` — explicit bounded concurrency;
- `foreach` — bounded durable fan-out over a resolved array;
- `while` — bounded durable iterative refinement with mandatory iteration ceiling;
- `playbook` — nested reusable procedure;
- `eval` — assertion/evaluation step.

## Skill resolver

Skill libraries remain external. The resolver understands standard `SKILL.md` packages and CometWeb's registry format.

A resolved lock commits to the **entire skill package tree**, including scripts, references and assets. Duplicate IDs across roots are ambiguity errors. Symlinks are rejected.

## Policy engine

Policies are executable constraints. Prompt text cannot override them. The engine controls allow/deny lists, approval classes, resolved-lock requirements, idempotency requirements, isolation requirements and budget ceilings.

## Persistence

The runner targets a `RunStore` contract rather than physical files. Two reference backends ship in core:

- filesystem: atomic plan/state snapshots, fsync'd JSONL events and file-backed lease/cancellation records;
- SQLite: transactional plan/state/event/lease/cancellation metadata with WAL + `synchronous=FULL`, while artifacts remain inspectable files.

`RunState v5` evolves through an explicit migration registry and carries parent/root lineage for forks, replays and subruns. Renewable TTL leases with heartbeat prevent concurrent mutation and make stale ownership recoverable. Lease loss during an in-flight material side effect is an uncertain-outcome event, not an ordinary failure.

Local artifacts may be content-hash verified. Optional attestation binds plan/state/event/artifact digests through a host-supplied signer; HMAC is only the reference signer.

## Secrets, plugins and capability admission

Sensitive playbook inputs remain references until immediately before invocation. Resolver plugins may bridge environment, files or external secret managers; the kernel redacts resolved secret markers before durable state/events/cassettes/telemetry.

Plugin discovery is metadata-only. Provider code is loaded after explicit host selection. Capability negotiation compares compiled requirements against runtime declarations before side effects; declarations are not security certification.

## State domains

Do not merge:

- **execution state** — where the run is now;
- **memory** — durable semantic/user context, external to the kernel;
- **evidence** — provenance-bound claims/references;
- **artifacts** — concrete files/objects;
- **approvals** — explicit human authorization records;
- **invocations** — durable attempts crossing a runtime boundary.

## Parallel semantics

Concurrency is explicit through `parallel`. Branches run concurrently up to `max_parallel`; branch-local dependency DAGs execute deterministically.

Nested step state is durable. Human/material approvals inside branches use stable path IDs such as `parent/branch/step`, allowing pause/resume without repeating already completed sibling work.

Cross-branch dependencies remain rejected because they imply synchronization outside the parent parallel contract.

## Observability and harness

The event log is the primary durable control-plane trace. Telemetry sinks receive redacted copies and are isolated from application success; a generic OpenTelemetry bridge can map events into host tracers. Golden/adversarial evals, fault injection, strict value/stream cassettes, replay, benchmark, conformance and run diffing exercise the kernel independently of any particular model provider.

## Provider neutrality

The core imports no OpenAI, Anthropic, Cursor, LangGraph, CrewAI or Microsoft agent SDK. Those may be adapters/backends, not architectural dependencies.

See `docs/adr/` for binding decisions.


## Adaptive loop semantics

Adaptive control flow remains bounded by deterministic ceilings. `foreach` limits total items and concurrency; `while` requires `max_iterations` and is additionally capped by execution/host policy. Nested steps receive stable durable identities, so approvals and resume do not replay completed work. A while condition that remains true after the ceiling is a failure, not implicit success.

## Promotion boundary

The kernel may observe ephemeral candidates, but reusable playbooks are not self-modified by a run. Promotion consumes a verified hash-chained observation registry, enforces configured success/diversity thresholds, and requires an explicit maintainer action that emits a promotion receipt bound to the registry head.
