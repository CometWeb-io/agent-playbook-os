# Host/provider adapters

Adapters translate the provider-neutral kernel into a concrete host/provider implementation. The core package intentionally has **no required OpenAI, Anthropic, Cursor or other provider SDK dependency**.

## Runtime responsibilities

A serious adapter keeps these concerns explicit:

1. **Skill execution** — execute a resolved Agent Skill without bypassing its lock/provenance contract.
2. **Agent execution** — run a bounded reasoning task and normalize provider output.
3. **Action execution** — execute tools/MCP/API calls with truthful side-effect, timeout and idempotency semantics.
4. **Approval presentation** — show kernel approvals through the host UI without minting authorization on behalf of the user.
5. **Isolation** — advertise `sandbox`/`subagent` only when the host provides a real matching boundary.
6. **Secret handling** — accept late-resolved secret values without logging/persisting them.
7. **Cancellation/cleanup** — cooperate with cancellation and surface ambiguous material outcomes as `UNKNOWN`.
8. **Schema resolution** — return pinned bytes to the kernel; never bypass hash verification.
9. **Provider receipts** — preserve native request/message/job IDs when the provider actually issued them.
10. **Streaming** — normalize partial usage/artifact/evidence/receipt events and end with one terminal result.

## Built-in bridges

The Python package includes thin, injected-client bridges:

- `OpenAIResponsesRuntime`
- `AnthropicMessagesRuntime`
- `CursorHostRuntime`
- `OpenAIPlannerRuntime` / `AnthropicPlannerRuntime` for typed model-backed planning with injected clients
- `CallablePlannerRuntime` for host-defined planning
- `CallableRuntime`

They demonstrate the contract without making vendor SDKs required dependencies. The OpenAI/Anthropic bridges intentionally do not claim downstream idempotency that they do not themselves configure/prove.

## Capability admission

Before a production run, compare a plan to the runtime:

```bash
playbook preflight playbooks/examples/kernel-demo.yaml --input name=Ada --dry-run
```

`--strict-capabilities` repeats the check inside the runner before execution state is created. Capability declarations remain claims; host/provider integration tests must prove them.

## Conformance

```bash
playbook conformance --dry-run
```

Conformance validates the structural runtime contract and basic semantics. It does **not** prove provider correctness, credential scope, sandbox strength, receipt truthfulness or remote idempotency.

## Adapter rules

- Never fabricate provider receipt IDs (`unknown`, random local UUIDs, etc.).
- Never copy a kernel idempotency key into provenance as if the provider honored it unless the request actually used/proved it.
- Preserve durable invocation identity across interruption/resume.
- Keep telemetry/export failures non-fatal to workflow execution.
- Keep credentials/raw secrets out of run snapshots and cassette files.
- Treat `UNKNOWN` material outcomes as reconciliation problems, not ordinary retries.


## Planner responsibilities

Planner adapters produce typed planning proposals only. They do not own execution permissions. Treat catalog descriptions/context as data, keep provider credentials in the host boundary, and preserve the OS sequence `proposal -> validate -> compile -> policy -> capability preflight -> plan approval -> run`.
