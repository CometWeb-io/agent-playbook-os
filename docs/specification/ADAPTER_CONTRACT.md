# Runtime adapter contract

The core is provider-neutral. A runtime adapter converts stable Agent Playbook OS invocation contracts into host-specific model/tool/skill execution without moving provider policy into the kernel.

Required async methods:

```text
execute_skill(step, inputs, context)
execute_action(step, inputs, context)
execute_agent(step, inputs, context)
execute_eval(step, inputs, context)
```

Each method may return:

1. a plain JSON-compatible value;
2. `RuntimeResult`;
3. an async stream of `RuntimeStreamEvent` ending in exactly one terminal `result` event.

Recommended capability methods:

```text
capabilities()
supports_isolation(mode, step) -> bool
supports_idempotency(step) -> bool
```

## RuntimeResult

The typed result can carry:

- canonical value;
- usage/cost counters;
- artifact refs;
- evidence refs;
- provider receipts.

The kernel persists only redacted/control-plane-safe material. Provider SDK objects should be normalized by the adapter before crossing this boundary.

## Provider receipts

When a provider supplies a native request/message/job/tool-call identifier, adapters should emit a `ProviderReceipt`. A useful receipt includes:

```text
provider
call_id
operation
status
idempotency_key       when actually honored downstream
payload_hash          when useful
metadata              redacted, bounded metadata only
```

Never invent a provider receipt ID when the provider did not issue one. A local invocation ID is not a substitute for a downstream acknowledgement.

## Streaming

A streaming adapter may emit progress, usage, artifact, evidence and receipt events before its terminal result. The engine aggregates them into the same durable run model used by non-streaming calls.

Requirements:

- exactly one terminal result;
- no accepted output after terminal result;
- cancellation/timeout must close or abandon the stream through the adapter's bounded cleanup policy;
- local/external/destructive uncertainty after dispatch must be represented as unknown outcome, not ordinary failure;
- streaming telemetry must not be the only durable record of a receipt/artifact.

## Truthfulness requirements

Adapters must not claim:

- `sandbox` when execution is merely inline;
- `subagent` when the same unisolated context performs the work;
- idempotency when the downstream provider does not honor the key;
- receipt support when native acknowledgement identity is unavailable;
- success before a material side effect has a confirmed outcome.

If certainty is lost after dispatching a material operation, the adapter should surface outcome uncertainty rather than a normal retryable failure.

Material operations include local writes. In-flight local, external and
destructive invocations interrupted by timeout, cancellation (including
cancellation of the runner task) or lease loss are recorded as `UNKNOWN`.
Resume requires reconciliation for every `UNKNOWN`, even if the step declared
`side_effects: none`, and for material `STARTED` records recovered after a
crash. A stable key alone is insufficient: the existing exception requires
adapter-proven downstream idempotency. Ordinary no-effect cancellation stays
`CANCELLED`. Completed steps are not repeated after reconciliation. See ADR-050.
Container retries apply the same gate to uncertain child invocations before
another attempt, even if the parent declares no side effects. An output-limit
interruption of an already dispatched material command also yields `UNKNOWN`;
invalid inputs rejected before dispatch remain ordinary failures.

## Capability negotiation

The compiler defines what a plan requires; the runtime declares what it can supply. `playbook preflight` compares both before side effects.

Strict runner mode repeats the capability preflight at execution time. A capability ID is unique within a registry; duplicate IDs are rejected.

## Deadlines for injected provider clients

The OpenAI/Anthropic runtime and planner adapters await async clients directly
and move synchronous SDK calls to a worker thread. This keeps timeouts,
cancellation and lease heartbeats responsive. Cancellation stops waiting for
the result; it does not terminate a worker thread or retract a remote request.
Configure finite network deadlines on injected SDK clients and prefer async
clients when available. An external/destructive step that times out remains
`UNKNOWN`; a late SDK response is not persisted as successful completion and
does not authorize a retry. SDK clients used synchronously must permit calls
from a worker thread.

Generic `CallableRuntime` handlers remain host-owned; async handlers must yield
and must not perform blocking I/O on the event loop.

Capability negotiation is admission control, not certification. Conformance tests and provider-specific integration tests remain required.

## Reference bridges

`ReferenceRuntime(dry_run=True)` returns structured plans for skill, agent and
command invocations. Commands still require explicit enablement, an allowlisted
executable and valid inputs; preview does not bypass admission checks. Command
previews contain `kind: command-invocation` and `dry_run: true`, not fabricated
stdout or a successful exit code. A downstream gate requiring real command
output must not treat a preview as execution evidence.

This is not a filesystem-wide read-only mode: the runner still persists state,
events and approvals, while reference `set`, `echo`, `sleep` and equality evals
retain their deterministic behavior. Injected host adapters must independently
honor their declared dry-run contract; the CLI reference flag cannot sandbox
arbitrary host code.

- `CallableRuntime`: injected skill/action/agent/eval handlers for hosts with their own APIs;
- `OpenAIResponsesRuntime`: injected OpenAI-compatible client, no SDK dependency in core;
- `AnthropicMessagesRuntime`: injected Anthropic-compatible client, no SDK dependency in core;
- `CursorHostRuntime`: injected host handlers; it does not pretend that Cursor exposes a stable Python SDK where none is configured.

## Conformance

Use:

```bash
playbook conformance --dry-run
playbook preflight playbooks/examples/kernel-demo.yaml --dry-run
```

Provider adapters should run the same conformance suite plus host-specific tests for permissions, cancellation, streams, native receipts, idempotency and isolation.


## Planner adapter contract

Planning is a separate provider boundary from execution. A planner implements:

```text
plan(PlannerRequest) -> PlannerResponse
```

The request contains a bounded goal, catalog metadata, context and constraints. Catalog/context are untrusted data. The response is typed as `existing | ephemeral | none`; it never directly executes work. Ephemeral output still goes through Playbook validation, compilation, host-policy composition, capability preflight and plan approval.

Reference injected-client bridges:

- `OpenAIPlannerRuntime`;
- `AnthropicPlannerRuntime`;
- `CallablePlannerRuntime`;
- `StaticPlannerRuntime` for deterministic host handoff/tests.

Planner adapters must not interpret a model response as authorization, silently select ambiguous catalog versions, or return an unbounded provider object into core.
