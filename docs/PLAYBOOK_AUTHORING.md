# Playbook authoring guide

## Start from a repeatable decision boundary

A playbook is justified when order, gates, evidence contracts, isolation or recovery behavior matter across repeated runs. Do not create a playbook for a single prompt that has no durable procedure.

## Prefer deterministic structure

Good:

```yaml
- id: tests
  type: action
  action: command
- id: release-gate
  type: gate
  gate: condition
  needs: [tests]
  condition: "steps.tests.output.returncode == 0"
```

Avoid asking a model whether a deterministic exit code should count as success.

## Keep specialist method in skills

A playbook may say `uses: evidence-researcher`. It should not copy the evidence-research methodology into the workflow definition.

## Declare data dependencies

Every `steps.foo` reference must be backed by `needs: [foo]`. This gives the compiler an auditable data-flow graph.

Within a parallel branch, local child dependencies are explicit. Cross-branch dependencies are intentionally rejected in v1alpha1.

## Classify side effects honestly

Use:

- `none` for pure computation/read-only interpretation;
- `local` for local filesystem/process mutations;
- `external` for APIs, messages, remote writes, PRs, CRM changes;
- `destructive` for delete, force, production or irreversible operations.

A host adapter may escalate the classification but should never downgrade it silently.

## Treat idempotency as an external guarantee

For retried material side effects, declare an `idempotency_key`. The kernel keeps it stable across attempts, but that alone is not enough: the adapter/downstream system must actually honor the key before the runtime advertises idempotency support.

If a material invocation times out, is cancelled, or crashes after dispatch, the safe result may be `UNKNOWN`. Design the integration so an operator or adapter can reconcile the invocation before resume.

## Use isolation deliberately

`inline`, `sandbox`, and `subagent` are different trust/execution boundaries. Use subagents for independent review, adversarial challenge, context isolation or bounded parallel specialist work—not role-playing.

If a playbook or policy requires `sandbox`/`subagent`, a runtime without that capability will fail closed.

## Sensitive inputs are references

For inputs marked `sensitive: true`, pass a secret reference rather than a raw credential, e.g.:

```yaml
spec:
  inputs:
    api_key:
      type: string
      required: true
      sensitive: true
```

Runtime input:

```text
api_key=env://SERVICE_API_KEY
```

The host resolves the reference. The raw secret should not become durable playbook state.

## Pin remote schemas

A remote `output_schema` requires `output_schema_hash` with the expected SHA-256. The host must provide a resolver; the kernel checks the returned bytes against the pin before validating output.

Prefer local schemas when practical.

## Parallel approvals are durable

Approvals/gates can exist inside parallel branches. Nested states use stable identities like:

```text
parent-step/branch/child-step
```

After approval/resume, already completed sibling work remains completed.


## Keep adaptive control flow bounded

Use `foreach` for finite fan-out and `while` for iterative refinement. Both are kernel control flow, not an excuse to hand scheduling to a model.

```yaml
- id: refine
  type: while
  condition: loop.iteration == 0 or loop.previous.outputs.review.passed == false
  max_iterations: 3
  do:
    - id: review
      type: eval
      with:
        candidate: "{{ inputs.draft }}"
```

Every `while` must declare `max_iterations`; the execution and host policy can impose a lower ceiling. If the condition remains true at the ceiling, the run fails instead of silently accepting an unfinished result.

`foreach` items and loop iterations have durable nested identities, so human approvals and resume do not replay completed work. Keep complex nested synchronization in sub-playbooks rather than stacking control constructs.

## Generated playbooks are proposals, not authority

A model-backed Operator can draft an ephemeral playbook, but the generated artifact must pass compile/policy/capability gates and receive explicit plan approval before execution. Prefer `prepare-only -> review -> run-plan` for consequential workflows so the exact reviewed compiled plan is what runs.

Host policy must be supplied again when running/resuming a persisted plan if organizational policy may have changed; a stricter current policy forces recompilation rather than silently mutating the immutable plan.

## Design for cancellation and resume

Assume any step can be interrupted. Keep external calls reconcilable, avoid hidden mutations, and never make correctness depend on an in-memory-only callback.

## Make outputs typed where it matters

Use `output_schema` for machine-consumed outputs. Keep domain schemas close to the capability that owns them; the OS validates and transports them.

## Promotion lifecycle

An operator may create an ephemeral plan, but reusable playbooks should be promoted through review:

`observed repeated pattern -> candidate playbook -> tests/evals -> recovery/adversarial tests -> maintainer review -> versioned release`
