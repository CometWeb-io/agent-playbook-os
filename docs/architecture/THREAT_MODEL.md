# Threat model

## Assets

- user data and credentials exposed through host tools;
- filesystem/repository integrity;
- external systems reachable through MCP/API connectors;
- approval integrity;
- budget and rate-limit integrity;
- run provenance and evidence lineage;
- playbook and skill supply chain;
- durable invocation identity and recovery state;
- provider acknowledgement/receipt lineage;
- migration/storage-backend integrity;
- secret resolver and plugin selection boundaries.

## Trust boundaries

The kernel trusts its Python process and configured persistence location. It does not automatically trust:

- playbook text;
- model/skill outputs;
- external tools;
- skill packages from arbitrary roots;
- remote schemas;
- host adapters;
- telemetry exporters;
- storage/secret/plugin implementations;
- provider-native receipt metadata;
- an isolation claim that the runtime cannot prove.

## Primary threats

### Prompt injection changes control flow

Mitigation: permissions, dependency edges, conditions, approvals and isolation are evaluated by the kernel. Model/skill text is data unless explicitly bound into a typed control field.

### Duplicate side effects during retry

Mitigation: policy requires stable idempotency keys for retried `external`/`destructive` side effects by default. Keys remain stable across attempts and are exposed to adapters. Adapters must explicitly declare idempotency support before the kernel treats an uncertain material invocation as retryable.

### Duplicate side effects after timeout, cancellation or crash

Mitigation: every invocation receives durable identity and lifecycle state. A material invocation whose real-world outcome is uncertain becomes `UNKNOWN`, not `FAILED`. Resume fails closed until the invocation is reconciled or the adapter can prove idempotent retry semantics.

### Concurrent resume executes the same work twice

Mitigation: a renewable TTL lease guards active mutation/execution and is refreshed by heartbeat. A second runner fails with a lease conflict while ownership is live. Lease loss interrupts local waiting. For an in-flight material side effect, the invocation becomes `UNKNOWN` rather than being retried optimistically.

### Approval replay or approval loss

Mitigation: typed approval records bind actor, timestamp and step identity. A resolved approval remains authoritative across resume for the same durable step boundary. Approval state is not re-derived from model output.

### Runaway cost or loops

Mitigation: compile-time step/attempt ceilings plus runtime wall-time, per-step, call, token and cost budgets. Known call-count limits are checked before invocation.

### Skill supply-chain drift

Mitigation: full-directory content hash, version/ref metadata, duplicate-ID ambiguity errors, optional `require_resolved_skills`, symlink rejection, and explicit lock verification.

### Hidden side effects

Mitigation: steps declare `side_effects`; policy can force human approval and minimum isolation. Host adapters must independently classify tools and reject mismatches rather than silently downgrade them.

### Isolation downgrade

Mitigation: `sandbox` and `subagent` are enforced runtime capabilities. If a requested or policy-required isolation mode is unavailable, execution fails closed instead of silently running inline.

### Arbitrary shell execution

Mitigation: command action is disabled by default, uses argv-only `create_subprocess_exec`, never a shell string, supports executable allowlists, bounded timeout and output-size ceilings.

Executable allowlist entries are resolved to canonical paths when the runtime
is configured. A different path with the same basename is not authorized, and
later PATH changes do not redirect a bare approved command. The configured
executables, their directories and arguments remain trusted; an interpreter
allowlist is not a sandbox or an argument policy.

Output is bounded while stdout/stderr are read, with one combined byte budget.
Cancellation, timeout and overflow stop/reap the child. On POSIX, commands own a
new session and cleanup kills their process group, including ordinary children.
Programs that deliberately detach into another session require a stronger host
sandbox. On non-POSIX systems the reference cleanup owns only the direct child;
process-tree containment requires a host adapter and has not been verified by
the macOS tests. Do not claim Windows job-object containment from this runtime.

### Untrusted expressions execute code

Mitigation: restricted AST interpreter. Function calls/imports/binary operations outside the allowed grammar are rejected. Expression character and AST-node budgets limit parser abuse.

### Oversized playbooks consume memory

Mitigation: reference loader rejects playbook files above a fixed size before parsing.

### Output schema declaration creates false assurance

Mitigation: local schemas are path-confined and validated. Remote schemas require a SHA-256 pin plus an explicit resolver; the fetched bytes must match the pin before validation. Unsupported remote validation fails closed.

### Subplaybook path escape

Mitigation: resolved child path must remain inside the parent playbook directory after symlink resolution.

### Secrets leak into durable state, cassettes, receipts or traces

Mitigation: sensitive inputs accept references rather than raw literals. Resolution happens immediately before invocation through an explicit resolver chain. Kernel-resolved values use a secret marker and are redacted from state outputs, provider receipt metadata, record/replay cassettes and telemetry. File-secret reads are root-confined. A malicious runtime/provider SDK can still leak values after receiving them, so adapter logging remains a separate boundary.

### Provider acknowledgement is mistaken for business success

Mitigation: native receipts are persisted as provenance linked to invocations/steps, but attaching or receiving a receipt does not automatically reconcile an `UNKNOWN` material operation. Final recovery must consult the authoritative downstream system.

### Runtime capability spoofing

Mitigation: capability negotiation can reject missing capabilities before side effects and the capability set is fingerprinted for resume drift detection. Residual risk remains because a malicious adapter can lie about a capability; provider-specific conformance/integration tests are required.

### Plugin discovery executes untrusted code

Mitigation: entry-point discovery returns descriptors without loading provider implementations. Explicit host selection is required before load/create. Duplicate names are ambiguity errors. Package installation itself remains an external supply-chain boundary.

### State migration silently changes resume semantics

Mitigation: released state shapes use explicit version-to-version migration functions; unsupported future schemas fail closed. Persisted migration is an explicit command and creates a source snapshot backup before rewrite.

### Telemetry failure changes application behavior

Mitigation: telemetry sinks are observability extensions. Exporter exceptions are isolated and must not change run success/failure semantics.

### Run/event/state tampering

Mitigation:

- exact compiled-plan integrity hash;
- stable semantic plan hash;
- run-state integrity hash;
- contiguous hash-chained event log;
- state/plan/event cross-reference verification;
- content hashes for local artifacts;
- optional HMAC run attestation over plan, state, event head and artifacts.

The local hashes are tamper-evident. HMAC attestation additionally authenticates the recorded state as long as the host protects the signing key and keeps it outside the run directory. This is not equivalent to external WORM storage or hardware-backed signing.

### Run directory accidental reuse

Mitigation: starting a fresh run in a directory containing run data fails; caller must resume or select a new run ID.

### Parallel branch resume repeats completed siblings

Mitigation: nested steps use stable `parent/branch/step` identities and independent durable state. Completed siblings remain completed when another branch pauses for approval or fails.


### Planner prompt injection or generated-policy downgrade

Mitigation: catalog descriptions and planning context are explicitly treated as untrusted data; planner output must parse into a bounded typed response. Ephemeral playbooks traverse the ordinary compiler. Host/org policy is composed monotonically and cannot be weakened by generated playbook policy. Newly generated plans require a distinct plan-level approval before executable state is created.

### Runaway adaptive loop

Mitigation: `foreach` has a hard item ceiling and bounded concurrency. `while` requires an explicit `max_iterations` and is capped again by execution/host policy. If its condition remains true at the ceiling, the step fails closed. Nested iteration state is durable so resume does not reset the loop budget by replaying completed iterations.

### Promotion feedback loop silently self-modifies production playbooks

Mitigation: observations are appended to a hash-chained registry; failures block default readiness; success/diversity thresholds are explicit; promotion is a separate maintainer action and emits a receipt bound to the registry head. A single successful run cannot rewrite a reusable playbook automatically.

## Residual risks

- A malicious/buggy production adapter can violate its declared contract despite conformance checks.
- Provider-reported token/cost usage can only be enforced after a call unless the provider supports reservation/preflight.
- Input/output state may contain sensitive domain data even when credentials are externalized; callers must classify durable data appropriately.
- Hash/HMAC authenticity is only as strong as storage and key separation.
- Filesystem/SQLite leases are reference/local coordination mechanisms; the kernel does not yet ship a distributed fencing-token lease/store for clustered workers.
- Nested `parallel`/`foreach`/`while` combinations remain deliberately constrained in v1alpha1; complex synchronization should be expressed as sub-playbooks until durable ownership semantics are specified.

## Non-goals

The reference kernel does not secure a compromised OS, malicious Python dependency, stolen provider credential, or administrator with unrestricted write access to both state and signing infrastructure.
