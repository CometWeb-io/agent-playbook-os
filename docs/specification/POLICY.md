# Policy model

Policies are executable constraints, not advisory prompts.

```text
policy > compiled playbook > operator > skill > action/tool
```

v0.6 supports:

- allowed step types;
- skill/action allow and deny lists;
- side-effect approval classes;
- idempotency requirements for retried side effects;
- mandatory runtime isolation per side-effect class;
- maximum total/nested steps, parallelism, foreach cardinality and loop iterations;
- optional requirement that used skills are locally resolved and content-locked;
- hard budget ceilings for wall/step time, attempts, invocations, model/tool calls, tokens and cost;
- strict runtime capability preflight before side effects;
- runtime capability-drift rejection on resume by default.

## Isolation policy

```yaml
policy:
  require_isolation_for:
    external: sandbox
    destructive: subagent
```

The runtime must advertise the boundary and the host must actually implement it. A playbook declaration is not proof that a sandbox or subagent exists.

## Budget composition

Execution may request a budget and policy may impose a ceiling. For every field, the effective limit is the stricter non-null value.

A playbook requesting a budget greater than policy ceiling is rejected at compile time. Known invocation-count limits are checked before calls. Provider-reported tokens/cost are checked after typed runtime/stream usage arrives.

## Skill locks

`require_resolved_skills: true` turns unresolved skill dependencies into a compile-time denial. Resolved locks cover the full package tree and reject symlinks/ambiguous IDs.

## Sensitive inputs

Inputs marked `sensitive: true` must be references rather than raw values:

```yaml
inputs:
  api_token:
    type: string
    sensitive: true
```

Accepted core schemes are `env://`, `secret://` and `file-secret://`. Resolution occurs immediately before runtime invocation. Resolved values are marked secret and redacted before durable output/event/receipt persistence.

The host still owns credential ACLs, rotation and access auditing.

## Capability policy

`playbook preflight` computes required vs offered capabilities without running the plan. Strict execution can require the preflight to pass before a run directory receives state.

Capability declarations are not an authorization mechanism. Provider permissions and external system scopes must be enforced by the adapter/tool boundary itself.


## Host policy overlay

The compiler may receive a host/org policy in addition to the playbook policy. Composition is monotonic: allowlists intersect, denylists and mandatory controls union, numeric ceilings take the stricter value, and host isolation requirements win direct conflicts. The compiled plan stores hashes for the playbook policy, host policy and effective policy.

This boundary is mandatory for model-generated playbooks: planner output cannot grant itself broader permissions, higher budgets or weaker approvals than the host allows.
