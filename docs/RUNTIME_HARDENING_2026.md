# Runtime hardening, threat boundaries, and release gates

**Scope:** post-v0.6 hardening of the reference control plane. This document describes
implemented controls, explicit limits, and tests. It is not a claim of production
certification, cloud availability, or live-provider conformance.

## Core invariants

1. **Expressions consume data, not Python objects.** The expression evaluator permits
   lookups into exact built-in dictionaries, lists, and tuples only. Host-object
   introspection, custom object attributes and private attribute traversal are denied.
   `with`, `when`, `branch`, and `gate` must share the same interpreter.
2. **The host controls the minimum side-effect classification.** A plan cannot claim
   that an action is harmless when its registered capability declares local/external/
   destructive effects. `command` is **local**, including read-only commands because
   the executable and its arguments can modify the filesystem.
3. **Injected host actions should declare effects.** `CallableRuntime` accepts
   `action_side_effects={"tool": "external"}`. With
   `Runner(enforce_capabilities=True)`, unclassified injected actions are rejected
   before a run directory is created. The host must own this descriptor registry.
4. **Unknown outcomes cannot be suppressed.** `SideEffectOutcomeUnknown`,
   `ReconciliationRequired`, integrity failures and lost leases are never converted
   into successful `on_fail=skip/continue` paths. Check downstream systems before
   retrying material side effects.
5. **Child playbooks are not new trust domains.** Their effective policy is a
   monotonic restriction of the parent policy. Child state survives human approval,
   can resume without repeating successful children, and is bounded by parent policy
   and available resource budgets. Existing child execution is bound to its original
   playbook content hash and persisted compiled plan.
6. **Queue ownership requires a live fence.** Expired claims cannot be acknowledged
   or renewed. A cancelled, expired claim becomes terminal `CANCELLED`. Eligible work
   can be discovered beyond the first 256 pending entries.
7. **Worker recovery verifies provenance.** An existing run can be recovered only when
   plan/state/event integrity and its tenant/namespace/submission identity match the
   claimed work. The queued CLI pins the plan integrity hash in its submission metadata.
8. **Recordings must not contain secret markers in the clear.** Redaction happens
   before serializing runtime results and streaming values. Numerical usage fields
   such as `input_tokens` remain numerical and replayable. Runtime exception text is
   not persisted in recording cassettes.
9. **A generated plan is approved by exact hash.** The CLI requires
   `--approve-plan --approved-plan-hash sha256:...` for a model-generated ephemeral
   plan; it rejects a changed regenerated plan. A plain boolean is not sufficient.
10. **Provider completion is explicit.** OpenAI Responses results must not have a
    non-completed status; Anthropic Messages requiring additional tokens/tool use
    must not be silently interpreted as a final completed answer.

## Host integration example

```python
runtime = CallableRuntime(
    actions={"publish": publish_handler},
    action_side_effects={"publish": "external"},
)
runner = Runner(runtime, enforce_capabilities=True)
```

The playbook must itself declare `side_effects: external`. The policy normally
requires approval for external side effects. Capability registration alone does not
verify that `publish_handler` is sandboxed, idempotent or immune to prompt injection;
the host must enforce its contract and provide its own integration evidence.

## Human approval lifecycle

Use `playbook operate ... --prepare-only` or the initial `PLAN_APPROVAL_REQUIRED`
response, review the exact generated YAML and compiled plan, and copy the semantic
hash into the next authorized `operate` invocation. For an unchanged reviewed
artifact, `run-plan` can be used without replanning. Verify that the actual
execution source is the reviewed compiled plan.

Nested approval IDs use paths such as `child/review` and
`parallel/branch/child/review`. A resumed child retains its own invocation journal,
and a prior child approval is not inferred from model text.

## Required tests before raising readiness claims

| Gate | Examples | Evidence required |
| --- | --- | --- |
| Expression sandbox | dunder introspection, objects with custom methods, subscript mutation | No host introspection |
| Effects and approvals | misclassified command, unclassified callable in strict mode | No unapproved dispatch |
| Recovery | capability-drift resume on JSON and SQLite, crash windows, unknown + skip | Valid event chain, no duplicate material effects |
| Child controls | parent allowlist vs child, child human approval and resume | No policy downgrade or rerun |
| Secrets | tenant resolver chain, model/stream/exception cassettes | No canary in durable data |
| Distributed | incompatible first page, cancelled stale claim, expired ack, foreign run | No starvation, stale ack or foreign recovery |
| Model adapters | truncated responses, tool calls, retries, changed model IDs | Live host-backed results, not just mocks |
| Quality | paired baseline/evals with frozen models and skill hashes | Independently reviewed outcome evidence |

Tests in `tests/test_runtime_hardening_2026.py` cover reference scenarios.
The existing repo workflow is unchanged; this project does not introduce
new CI/CD infrastructure as part of this hardening pass.

## Known limits and non-goals

- Filesystem + SQLite remain reference local backends, not a certified cloud
  distributed store. Production claim needs fault injection and multi-host testing.
- Callable runtime descriptors are host claims. A malicious Python handler can lie
  about its effects, isolation, idempotency, provider receipts and output values.
- `on_fail=skip/continue` may still mask ordinary business failures. A run marked
  `COMPLETED` is **not** by itself evidence that the requested business outcome occurred.
- SQLite emitted events and their corresponding snapshots now share one atomic
  transaction and a fault-injection regression. Separate **pre-dispatch journal**
  snapshots are intentionally persisted ahead of an event; filesystem stores and
  other direct state mutations are still not globally atomic. Crashed windows
  must fail closed and be reconciled. This does **not** establish a full
  event-sourced transactional workflow engine.
- Secret classification based on runtime markers is not full information-flow
  security. Downstream SDK logging, custom objects and raw literal secrets require
  host-level controls.
- The OpenAI/Anthropic bridges remain limited injected-client adapters. They do not
  implement complete vendor-native tool loops, OAuth, delegated permissions,
  network isolation, or provider-backed sandbox conformance.
- Real quality evaluation, provider live smoke and durable multi-host tests remain
  release blockers. Never claim all-provider support from reference/mocked tests.

## Roadmap after safety gate

1. Complete transactional coverage of all state mutations (including pre-dispatch
   journals), add power-loss and multiprocess fault injection for every boundary.
2. Pin transitive subplaybooks at compile time. Local output JSON Schemas are
   now SHA-256 pinned in compiled plans when a source path is supplied.
3. Introduce host-authenticated action registries, short-lived permission grants,
   provider-native reconciliation and explicit approval actor verification.
4. Add production store adapters only behind the same durability/fencing conformance.
5. Run real OpenAI and Anthropic scenarios (including incomplete outputs, MCP tools,
   approvals, cancellation, retries and receipts) and paired quality benchmarks.
6. Add tracing through established OpenTelemetry GenAI conventions, not a proprietary
   duplicate telemetry protocol.


## October 11 reliability expansion

- **Queue status correctness:** `ack({"status": ...})` preserves
  `WAITING_APPROVAL`, `FAILED` and `CANCELLED`, rather than labelling every
  acknowledged attempt as completed.
- **SQLite atomic emitted transitions:** every Runner event and accompanying
  snapshot share a single transaction; tested with an injected SQLite abort.
  Full state-write atomicity remains a separate release gate.
- **Compiler local schema integrity:** local JSON Schema contents are pinned
  during compilation without mutating source playbooks, and verified again
  during execution. Remote schema references still require explicit pins.
- **Provider continuation/refusal:** tool calls and incomplete responses are
  first-class non-retryable conditions in the reference adapters, not final
  answers. If the playbook declared possible material effects, a nonterminal
  provider response creates an UNKNOWN invocation pending reconciliation.
- **Streaming durability:** partial token usage, artifacts, evidence and
  provider receipts are saved when emitted; stream errors no longer erase them.
  Budget enforcement happens after each usage event and makes already-started
  material effects UNKNOWN on budget termination.
- **Tenant secrets:** securely open each tenant/namespace directory component
  using POSIX no-follow handles; reject symlinks, nonregular files and oversize
  payloads. Non-POSIX tenant-file reads fail closed pending a verified OS-native
  implementation.
- **Lease safety:** locally alive PIDs remain leased despite heartbeat delays.
  Dead-owner filesystem reclaim is serialized with POSIX flock. Remote-owner
  recovery is deliberately manual/fail-closed; these stores remain reference
  adapters, not distributed lease services.
- **Inspection:** reports `requires_attention`, `risk_flags` and explicitly
  `business_outcome_verified=false`, because completion/receipts are not
  downstream truth.
- **Concurrency:** independent worker threads contend for the SQLite queue in
  a duplicate-claim test. This is not a multi-host deployment certification.

### Remaining high-impact gaps

1. Production-grade Postgres/managed queue and durable remote artifact backend
   with cross-host fencing enforced on every material write.
2. Full provider-managed tool loops, MCP authorization, receipt reconciliation,
   sandboxes and live model evals for specific released adapter versions.
3. Robust end-to-end output quality benchmarks against matched legacy baselines.
4. Auditable human identity and cryptographic approval grants, tenant IAM and
   retention/privacy policy across observability and durable outputs.
5. Atomic handling of pre-dispatch journaling and filesystem state/event windows.
