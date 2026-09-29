# Changelog


## 0.6.0 - 2026-09-22

### Distributed execution

- Added typed work submission/item/stats contracts and a transactional SQLite reference work queue.
- Stable `work_id` is now a semantic idempotency key; conflicting reuse is rejected.
- Added capability-aware worker routing derived from `CompiledPlan` requirements.
- Added visibility timeouts, stale-claim reclamation, bounded retry, cancellation and DEAD terminal state.
- Added monotonically increasing claim fences and an independent run-resource fencing coordinator.
- Added concurrent reference workers with queue/fence heartbeats and safe completed-run recovery.
- Added queue list/stats/cancel/conformance and worker CLI operations.

### Multi-tenant boundaries

- Added validated tenant/namespace scopes and tenant-confined queue run paths.
- Added monotonic root -> tenant -> namespace policy resolution.
- Added `tenant-secret://NAME` resolver with single-segment and symlink restrictions.
- Added optional worker confinement for queued plan/run filesystem roots.

### Artifact provenance

- Added external artifact-store protocol and tenant-scoped `artifact+file://` reference backend.
- Run verification and attestation now fail closed for external artifacts unless an explicit verifier is supplied.
- Added hash and tenant/namespace validation for external artifact references.

### Conformance and architecture

- Added semantic distributed queue conformance probes for idempotency, capability routing, claim fencing and bounded retries.
- Added ADR-044 through ADR-049 and dedicated distributed/tenancy/artifact-store documentation.
- Exported machine-readable schemas for work submissions/items, queue stats, tenant scope and fencing leases.


## 0.5.0 - 2026-09-22

### Operator and adaptive planning

- Added a typed, provider-neutral model-backed Operator contract that can select an installed catalog playbook or propose an ephemeral candidate.
- Planner catalog/context are treated as untrusted data, planner requests/responses are content-hashed, response size is bounded, and injected OpenAI/Anthropic planner adapters avoid provider dependencies in core.
- Ephemeral plans must pass the normal Playbook model, compiler, host-policy overlay and runtime capability preflight before they are executable.
- Operator-generated ephemeral plans require an explicit plan-level approval before execution; review artifacts are persisted without creating executable run state.
- Added `playbook run-plan` so an inspected compiled plan can execute without calling the planner again.
- Added host/org policy composition: a generated or authored playbook may make policy stricter but cannot weaken host-owned allowlists, approvals, isolation or budget ceilings.

### Durable adaptive control flow

- Added bounded `foreach` fan-out with durable per-item state, concurrency ceilings, approvals, retries and resume.
- Added bounded `while` refinement loops with mandatory `max_iterations`, durable per-iteration state, nested approval/resume and policy ceilings.
- Loop exhaustion fails closed when the condition remains true at the configured limit.
- Boolean expressions now implement short-circuit `and`/`or`, allowing safe guards around optional loop/context values.

### Promotion and learning loop

- Added a hash-chained promotion registry for evidence-backed `ephemeral -> reusable` playbook promotion.
- Promotion readiness can require multiple successful runs and distinct goals; any failed/cancelled observation blocks default promotion.
- Promotion receipts bind the promoted artifact to the current registry chain head and support explicit new ID/version assignment.
- Added `candidate record/status/verify/promote` CLI operations.

### Lineage and differential evaluation

- `RunState v5` adds parent/root lineage, lineage depth and fork reason while retaining explicit v2 -> v3 -> v4 -> v5 migration.
- Added replay/fork lineage semantics and CLI inspection.
- Added differential replay lab reports with output consistency, status, latency and cost deltas across runtime/model variants.
- Added a bounded-refinement reference playbook and executable loop/adversarial eval cases.
- Eval cases can now expect schema/load-time validation failures as well as compiler failures.


## 0.4.0 - 2026-09-21

### Added

- `RunState v4` and explicit v2 -> v3 -> v4 migration registry with backup-before-persisted-migration.
- Pluggable durable store contract with filesystem and transactional SQLite reference stores.
- Renewable TTL leases with heartbeat and fail-closed lease-loss handling.
- Runtime capability fingerprint and resume drift protection.
- Capability negotiation / strict preflight before execution side effects.
- Plugin discovery descriptors for runtimes, stores, telemetry, secrets and schema resolvers.
- Late secret-resolution boundary for `env://`, `secret://` and root-confined `file-secret://`.
- Typed streaming runtime lifecycle with progress/usage/artifact/evidence/receipt/result events.
- Typed provider-native receipts linked to invocation and step state.
- Manual provider-receipt attachment for post-crash investigation without implying success.
- Injected-client OpenAI Responses and Anthropic Messages runtime bridges plus Cursor host bridge.
- Generic OpenTelemetry sink and composite telemetry sink.
- Generic attestation signer interface for KMS/HSM/Vault-style host bridges while preserving HMAC reference signing.
- Operational CLI: `inspect`, `migrate-state`, `receipts`, `uncertain`, `receipt attach`, `preflight`, `plugins list`, `capabilities`.
- Replay and benchmark support for selected storage backends and strict runtime admission.
- Reproducible release builder with `SOURCE_DATE_EPOCH`, normalized ZIP metadata and duplicate-build SHA verification.
- ADR-027 through ADR-035 covering migrations, stores, heartbeat leases, provider receipts, secrets, streaming, capability negotiation, external signers and plugin loading.

### Changed

- Runtime calls may return plain values, `RuntimeResult`, or typed async streams.
- Lease loss during material side effects now interrupts local waiting and records `UNKNOWN`.
- Provider receipt metadata is redacted before persistence.
- Duplicate capability IDs are rejected rather than silently overwritten.
- Operational commands auto-detect filesystem vs SQLite run storage.
- State migration preserves the actual storage backend.
- Replay now honors storage, secret resolver, lease and strict-capability options.
- Benchmark runner accepts a configured Runner factory so CLI storage/policy/runtime options are real execution settings.

### Security

- Secret values resolved by the kernel are marked and redacted from durable step output, receipts, cassettes and telemetry.
- File-secret reads are constrained to configured roots and size limits.
- Plugin discovery does not automatically import/authorize execution providers.
- Duplicate manual plugins and ambiguous migration graph branches are rejected.
- Provider bridges do not fabricate receipt IDs or imply provider idempotency without downstream proof.
- Relative file-secret paths resolve only inside configured roots and reject root ambiguity/path escape.
- Strict capability admission can fail before state creation when a runtime cannot satisfy the compiled plan.

## 0.3.0 - 2026-09-21

### Added

- `RunState v3` with explicit v2 snapshot migration.
- Durable invocation journal with stable invocation IDs and result hashes.
- `UNKNOWN` outcome for interrupted external/destructive side effects plus manual reconciliation.
- Per-run execution lease preventing concurrent create/resume races.
- Cooperative cross-process cancellation marker and cancellation CLI.
- Durable nested parallel-step state and approval/resume inside parallel branches.
- Runtime-enforced `sandbox` / `subagent` capability checks and policy-required isolation.
- SHA-256-pinned remote output schema contract with explicit resolver adapters.
- Secret-reference-only sensitive playbook inputs.
- JSONL telemetry sink with redaction and non-fatal exporter semantics.
- Strict runtime record/replay cassettes.
- `CallableRuntime` host integration bridge.
- Runtime conformance report and benchmark runner.
- Optional HMAC run attestation covering plan, state, event head and artifacts.
- CLI commands for cancellation, reconciliation, lease inspection/break, conformance, benchmark and attestation.
- Executable adversarial eval corpus with expected compile-rejection assertions.
- Benchmark approvals so gated workflows can be measured without being misclassified as failures.

### Changed

- Parallel approval-bearing steps are now supported instead of rejected at compile time.
- An already-recorded approval remains authoritative across failed-run resume.
- Remote schema URLs without a content hash pin now fail at compile time.
- Runtime isolation labels are treated as enforceable capabilities, not descriptive metadata.
- Timeout/cancellation of material side effects no longer collapses uncertainty into ordinary failure.

### Security

- Uncertain material side effects fail closed before resume unless reconciled or adapter-proven idempotent.
- Concurrent runners are rejected by a durable lease.
- Remote schema validation is pinned and never fetched implicitly by the reference kernel.
- Sensitive inputs cannot be embedded as raw secret values.
- HMAC attestation can authenticate an otherwise recomputable hash-chain snapshot.

## 0.2.0 - 2026-09-21

### Added

- Stable compiled-plan `semantic_hash` alongside exact `integrity_hash`.
- Run-state integrity hashes and full `playbook verify` cross-checks.
- Typed approval records with actor/timestamp and `gate.approved` trace events.
- Execution/policy budgets for wall time, per-step time, attempts, call classes, model/tool calls, tokens and cost.
- Per-step timeouts, retry filters, exponential backoff and stable idempotency context.
- Typed `RuntimeResult` carrying usage, artifact refs and evidence refs.
- Branch-local dependency execution inside explicit parallel steps.
- Whole-skill package hashing, symlink rejection, duplicate-ID ambiguity detection and lock verification.
- Catalog content hashes and same-version collision linting.
- Golden eval runner, run diffing, replay provenance and deterministic fault injection.
- Reference runtime capability registry, safe `sleep` action and command executable allowlist/output bound.
- Release-check and release-build scripts plus expanded machine-readable schemas.

### Changed

- Material side-effect retries fail compilation without an idempotency key by default.
- Remote output schemas fail closed when no resolver adapter exists.
- Fresh runs refuse to reuse a directory containing existing run data.
- Resume verifies event chain, plan integrity, state integrity and cross-hashes before executing.
- Expression evaluator now supports hyphenated step IDs and enforces expression size/AST limits.
- Loader rejects oversized playbooks before parsing.
- `on_fail: continue` failures may be inspected by downstream declared dependencies.

### Security

- Event sequence and run-ID continuity are verified in addition to hash linkage.
- Reference command runtime kills timed-out child processes and never invokes a shell.
- Full skill package content, not only `SKILL.md`, is supply-chain locked.

## 0.1.0 - 2026-09-21

- Initial reference kernel.
- Declarative playbook spec, compiler, runner, policy engine and persisted run state.
- Skill resolution compatible with standard SKILL.md layouts and CometWeb agent-skills registry.
- Resume, trace, replay primitives, human gates, branch/parallel/subplaybook steps.
- Security/adversarial and conformance tests.
