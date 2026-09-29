# Operator, ephemeral plans and promotion

The Operator is an advisory planning layer above the deterministic compiler/runner. It is **not** a privileged bypass around policy, approvals or capabilities.

## Planning contract

A planner returns exactly one typed mode:

- `existing` — select an installed catalog playbook by identity/version;
- `ephemeral` — propose a complete `playbook.agent/v1alpha1` candidate;
- `none` — decline when no defensible plan exists.

Catalog metadata and planning context are untrusted data. Planner output is size-bounded, parsed into `PlannerResponse`, and content-hashed. Unknown/ambiguous catalog identities fail closed.

## Ephemeral execution gate

An ephemeral candidate follows this sequence:

```text
planner response
  -> Playbook validation
  -> host/playbook policy composition
  -> compile + dependency/skill locks
  -> capability preflight
  -> persist review artifacts
  -> PLAN APPROVAL
  -> Runner
```

Model generation never implies execution authorization. Without explicit plan approval, no executable `RunState` is created. The persisted generated playbook, decision, gate report and compiled plan can be reviewed independently. `playbook run-plan` executes that exact compiled artifact after review without re-planning.

## Host policy precedence

Host/org policy is composed with playbook policy monotonically:

- allowlists intersect;
- denylists union;
- approval/idempotency requirements union;
- numeric ceilings take the minimum;
- host isolation requirements win direct conflicts.

A playbook or model can therefore tighten a host policy but cannot relax it.

## Promotion

Promotion is an evidence lifecycle, not self-modification. Candidate observations are appended to a hash-chained registry. Default readiness requires successful observed runs, no failed/cancelled observations and the configured number of distinct goals.

```text
ephemeral candidate
  -> observed runs
  -> registry verification
  -> readiness thresholds
  -> human/maintainer promotion
  -> new reusable playbook version
```

Promotion removes `ephemeral` / `operator-generated` tags and writes a promotion receipt containing the source candidate hash, promoted identity and registry chain head. The kernel never rewrites a production playbook in place as a side effect of one run.
