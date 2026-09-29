# Agent Playbook OS

A provider-neutral **control plane for reliable AI work**.

Agent Playbook OS lets one primary operator compile and execute versioned playbooks across reusable skills, deterministic actions, policy gates, human approvals and optional isolated subagents — without turning every responsibility into a separate autonomous agent.

> **Operator thinks. Playbooks sequence. Skills know how. Tools act. Policies constrain. State remembers. Evals verify.**

## Why this exists

Most agent stacks blur reasoning, procedure, execution, permissions, memory and evidence. That makes systems difficult to reproduce, debug, compare, resume safely or audit after the fact.

Agent Playbook OS separates those concerns:

```text
USER / EVENT / API
        |
        v
+--------------------+
|      OPERATOR      |  intent, selection, bounded planning
+---------+----------+
          |
          v
+--------------------+
| PLAYBOOK COMPILER  |  schema, deps, locks, policies, immutable plan
+---------+----------+
          |
          v
+--------------------+
|   RUNNER / ENGINE  |  checkpoint, retry, budgets, gates, recovery
+---------+----------+
          |
   +------+------+--------+----------+
   |             |        |          |
 SKILL         ACTION    GATE      SUBPLAYBOOK
   |             |        |
   +-------------+--------+
                 |
 STATE / EVENTS / INVOCATIONS / ARTIFACTS / EVIDENCE
                 |
 TRACE / EVAL / REPLAY / DIFF / ATTESTATION
```

Multi-agent execution is an **execution strategy**, not the architecture. Isolation is requested only where independent context, adversarial review, sandboxing or parallelism adds value.

## Current status

`v0.6.0` adds a distributed execution profile on top of the adaptive v0.5 kernel: idempotent work submission, capability-aware queue admission, claim fencing, run-resource fencing, concurrent workers, tenant/namespace policy scopes, tenant-confined secrets/run paths, and fail-closed external artifact verification. The optional Agent Skills boundary and comparison harness are structural integrations; they do not claim live Claude, Codex or ChatGPT host support.

### Compile and control

- declarative YAML/JSON playbooks with strict Pydantic validation and exported JSON Schemas;
- compile-before-execute immutable `CompiledPlan`;
- stable `semantic_hash` plus exact-artifact `integrity_hash`;
- explicit dependencies, cycle rejection and branch-local DAG validation;
- step types: `skill`, `action`, `agent`, `gate`, `branch`, `parallel`, `foreach`, `while`, `playbook`, `eval`;
- deterministic expression evaluation without Python `eval` or function calls;
- policy precedence over model/operator instructions;
- provider-neutral runtime and storage contracts.


### Operator, adaptive control flow and promotion

- typed model-backed Operator: select an installed playbook, draft an ephemeral candidate, or decline;
- planner request/response hashes, bounded context/response size and untrusted-data prompting;
- ephemeral candidates traverse validation -> compile -> host-policy composition -> capability preflight before review;
- explicit **plan-level approval** is required before an Operator-generated workflow executes;
- `run-plan` executes the exact reviewed compiled artifact without re-planning;
- host/org policy is monotonic: generated playbooks can tighten but never weaken it;
- durable bounded `foreach` fan-out and bounded `while` refinement loops with per-item/iteration approval + resume;
- hash-chained promotion registry for observed `ephemeral -> reusable` lifecycle;
- promotion readiness can require multiple successes and distinct goals, with failures blocking default promotion;
- run lineage/forks plus differential replay lab for model/runtime comparison.

### Distributed execution and tenancy

- typed `WorkSubmission`, `WorkItem`, `QueueStats`, `TenantScope` and `FencingLease` contracts;
- transactional `SQLiteWorkQueue` semantic reference with idempotent submission, visibility timeouts, bounded retry/dead-letter state and cancellation;
- capability-aware claim routing derived automatically from the immutable `CompiledPlan`;
- monotonically increasing queue claim fences plus an independent run-resource `SQLiteFencingCoordinator`;
- concurrent reference worker with queue/fence heartbeats, crash recovery and completed-run deduplication;
- optional `allowed_run_root` / `allowed_plan_root` confinement before queued filesystem access;
- tenant -> namespace policy inheritance that can only tighten host policy;
- `tenant-secret://NAME` resolver scoped to one authenticated tenant/namespace;
- external `ArtifactStoreProtocol`, tenant-scoped `artifact+file://` reference backend and fail-closed verifier registry;
- distributed queue conformance probes covering idempotency, capability routing, fencing and bounded retries.

See [distributed execution](docs/DISTRIBUTED_EXECUTION.md), [tenancy](docs/TENANCY.md) and [artifact stores](docs/ARTIFACT_STORES.md).

### Durable execution and storage

- persisted **`RunState v5`** with explicit v2 -> v3 -> v4 -> v5 migration registry and run lineage;
- inspectable filesystem store plus transactional SQLite reference backend;
- explicit state migration with source snapshot backup;
- pause/resume, failed-run recovery and replay to a different backend;
- renewable TTL lease with heartbeat and fail-closed lease-loss handling;
- cooperative durable cancellation;
- durable nested state and approvals inside parallel branches;
- typed approval records with actor/time;
- runtime-capability fingerprint and resume drift detection.

### Invocation safety and provider provenance

- durable invocation journal with stable invocation IDs;
- explicit `UNKNOWN` outcome for material side effects interrupted after possible dispatch;
- reconciliation gate before unsafe resume;
- provider-native `ProviderReceipt` records linked to invocation and step state;
- manual receipt attachment for post-crash investigation without falsely marking success;
- adapter-proven idempotent retry path;
- strict runtime record/replay for both normal results and streams.

### Secrets, capabilities and plugins

- sensitive inputs persist references, not credentials;
- resolver chain for `env://`, host-provided `secret://` and root-confined `file-secret://`;
- resolved values are marked secret and redacted before durable kernel persistence;
- runtime capability registry and `playbook preflight`;
- strict capability admission before execution side effects;
- plugin descriptors for runtimes, planners, stores, telemetry, secrets and schema resolvers;
- plugin discovery does not implicitly import/authorize provider code.

### Streaming and adapters

- runtime methods may return a value, typed `RuntimeResult`, or typed async stream;
- stream events for progress, usage, artifacts, evidence, receipts, logs and terminal result;
- `CallableRuntime` generic host bridge;
- injected-client OpenAI Responses and Anthropic Messages adapters without vendor dependencies in core;
- explicit Cursor host bridge rather than pretending an undocumented SDK exists;
- `CompositeTelemetrySink` and optional OpenTelemetry bridge.

### Safety and supply chain

- bounded retry, exponential backoff and retry-class filters;
- mandatory idempotency keys for retried material side effects by default;
- wall-time, per-step, attempt, invocation, token and cost budgets;
- runtime-enforced isolation (`sandbox` / `subagent`) with fail-closed behavior;
- remote JSON Schema requires SHA-256 pin plus explicit resolver;
- whole-package Agent Skill locks covering scripts/references/assets;
- duplicate skill/plugin/capability ambiguity detection and skill symlink rejection;
- safe reference command runtime: argv-only, no shell, a required non-empty executable allowlist and output bounds. Enabling `allow_commands=True` without `allowed_commands` fails closed.

### Provenance, observability and harness

- contiguous SHA-256 event chain;
- state/plan/artifact integrity verification;
- optional run attestation with local HMAC or host-injected signer boundary for KMS/HSM/Vault integration;
- redacted telemetry whose exporter failures cannot change workflow success;
- run inspection, trace, replay and diff commands;
- runtime conformance, benchmark runner, golden/adversarial evals and deterministic fault injection.

The kernel does **not** claim that a passing conformance suite makes an arbitrary provider adapter, sandbox or external system safe. Credentials, provider permissions, downstream idempotency and host isolation remain separate trust boundaries.

Agent Playbook OS is the control plane; [CometWeb Agent Skills](https://github.com/CometWeb-io/agent-skills)
remains the external capability library. Keep `SKILL.md` packages and their
domain evals in that repository. Inject a host-specific adapter only after its
identity, permissions, isolation, receipts and failure behavior have evidence.
See [release readiness](docs/RELEASE_READINESS.md) for the current boundary.

## Quick start

```bash
python -m pip install -e ".[dev]"

playbook validate playbooks/examples/kernel-demo.yaml --input name=Ada
playbook compile playbooks/examples/kernel-demo.yaml --input name=Ada --out /tmp/plan.json
playbook run playbooks/examples/kernel-demo.yaml --input name=Ada --run-root .playbook-runs
```


Use the Operator with a host/model planner response and review a newly generated plan before execution:

```bash
playbook operate "review this release and decide the next safe action" \
  --planner-response planner-response.json \
  --prepare-only

# After reviewing the persisted compiled plan:
playbook run-plan .playbook-runs/<RUN_ID>/operator/compiled-plan.json \
  --strict-capabilities
```

Or explicitly approve the generated plan in the same Operator flow:

```bash
playbook operate "review this release" \
  --planner-response planner-response.json \
  --approve-plan
```

Inspect and verify a run:

```bash
playbook status .playbook-runs/<RUN_ID>
playbook inspect .playbook-runs/<RUN_ID>
playbook trace .playbook-runs/<RUN_ID>
playbook verify .playbook-runs/<RUN_ID>
```

Run on SQLite and preflight capabilities before any execution:

```bash
playbook preflight playbooks/examples/kernel-demo.yaml \
  --input name=Ada --dry-run

playbook run playbooks/examples/kernel-demo.yaml \
  --input name=Ada \
  --approve approval \
  --store sqlite \
  --strict-capabilities
```

Operational commands auto-detect the persisted backend from the run directory.

Submit a compiled plan to the reference distributed queue:

```bash
playbook compile playbooks/examples/kernel-demo.yaml --input name=Ada --out /tmp/demo-plan.json
playbook queue enqueue /tmp/demo-plan.json --queue-db .playbook-os/work.sqlite3 --tenant acme --namespace prod
playbook worker once --queue-db .playbook-os/work.sqlite3 --allowed-run-root .playbook-runs --allowed-plan-root /tmp --strict-capabilities
playbook queue stats --queue-db .playbook-os/work.sqlite3
```


Resume a human gate with an auditable actor:

```bash
playbook resume .playbook-runs/<RUN_ID> \
  --approve approval \
  --approval-actor maciek@example.com
```

Nested approval IDs are paths, for example:

```text
independent-reviews/external/publish
```

## Secrets are resolved at the last responsible moment

Playbooks store references instead of raw credentials:

```text
env://OPENAI_API_KEY
secret://production/crm-token
file-secret://service/token
```

Resolution occurs immediately before the runtime invocation. The reference CLI always includes environment resolution and can add confined file-secret roots:

```bash
playbook run playbooks/examples/kernel-demo.yaml \
  --file-secret-root /run/secrets
```

Host secret managers can implement the same resolver contract for `secret://`. The kernel redacts values it resolves, but a malicious provider adapter can still leak a secret after receiving it; adapter logging remains a separate trust boundary.

## Streaming and provider receipts

Adapters may return a final value/result or an async stream of typed events. Usage, artifacts, evidence and native provider receipts are accumulated into durable state before the terminal result.

List provider acknowledgements attached to a run:

```bash
playbook receipts .playbook-runs/<RUN_ID>
playbook uncertain .playbook-runs/<RUN_ID>
```

After a crash, a native acknowledgement can be attached without pretending the operation succeeded:

```bash
playbook receipt attach .playbook-runs/<RUN_ID> <INVOCATION_ID> \
  --provider openai \
  --call-id resp_123 \
  --operation responses.create \
  --actor ops
```

Final outcome still belongs to reconciliation against the system of record.

## Pluggable storage and state migrations

The reference runtime ships two stores:

```text
filesystem   inspectable atomic JSON/JSONL snapshots
sqlite       transactional metadata/events in run.sqlite3 + filesystem artifacts
```

Persist a supported old snapshot into the current schema only through the explicit migration operation:

```bash
playbook migrate-state .playbook-runs/<RUN_ID> --actor ops
```

The source snapshot is backed up before migration. Replay can target a different store without mutating the source run.

## Plugins and capability negotiation

Discover installed integration descriptors:

```bash
playbook plugins list
playbook plugins list --kind runtime
playbook capabilities --dry-run
```

`preflight` compares plan requirements with runtime capabilities. `--strict-capabilities` repeats admission at run time before durable execution side effects. Capability declarations are not permission grants or security certification.

## Cancellation and recovery

Request cancellation from another process:

```bash
playbook cancel .playbook-runs/<RUN_ID> \
  --reason "operator requested stop" \
  --actor maciek
```

For `none/local` operations, cancellation is terminal and explicit. For an in-flight `external/destructive` side effect, the invocation is persisted as `UNKNOWN`: the kernel does **not** pretend it knows whether the external system committed the operation.

Inspect state and reconcile only after checking the system of record:

```bash
playbook reconcile .playbook-runs/<RUN_ID> <INVOCATION_ID> \
  --outcome not-executed \
  --actor ops
```

Or, when the operation is confirmed to have succeeded, provide the canonical result:

```bash
playbook reconcile .playbook-runs/<RUN_ID> <INVOCATION_ID> \
  --outcome succeeded \
  --output '{"external_id":"abc-123"}' \
  --actor ops
```

Then resume normally. See [Recovery and reconciliation](docs/specification/RECOVERY.md).

## Safe side effects and retries

A retry is another execution. For material side effects the default policy rejects retry plans without a stable idempotency key:

```yaml
- id: create-customer
  type: action
  action: crm.create-customer
  side_effects: external
  idempotency_key: "{{ inputs.request_id }}"
  retry:
    max_attempts: 3
    backoff_seconds: 1
    backoff_multiplier: 2
    retry_on: [TimeoutError, ConnectionError]
```

The key alone is not proof of idempotency. After an uncertain outcome, retry is automatic only when the runtime adapter explicitly reports that it supports idempotency for that step.

## Runtime isolation

Playbooks can request an execution boundary:

```yaml
- id: adversarial-review
  type: agent
  execution_isolation: subagent
```

Policies may require a stronger boundary for material side effects:

```yaml
policy:
  require_isolation_for:
    external: sandbox
    destructive: subagent
```

If the runtime cannot prove the requested boundary, execution fails closed. `auto` never silently upgrades itself to a sandbox.

## Pinned output schemas

Local schema:

```yaml
output_schema: schemas/result.json
output_schema_hash: sha256:<digest>
```

Remote schema:

```yaml
output_schema: https://schemas.example.com/result.json
output_schema_hash: sha256:<digest>
```

Remote schemas require both the hash pin and a host-supplied resolver. The reference CLI does not fetch remote schemas implicitly.

## Record and replay

Record runtime invocations:

```bash
playbook run playbooks/examples/kernel-demo.yaml \
  --input name=Ada \
  --record-cassette /tmp/run.cassette.jsonl
```

Replay against the exact recorded invocation signatures:

```bash
playbook replay .playbook-runs/<RUN_ID> \
  --replay-cassette /tmp/run.cassette.jsonl
```

A signature/order mismatch fails instead of silently consuming the wrong recorded output.

## Provenance attestation

The event chain is tamper-evident but not an authenticity proof by itself. For controlled environments, sign a run summary with HMAC:

```bash
export APBOS_ATTEST_KEY='use-a-secret-from-your-secret-manager'
playbook attest .playbook-runs/<RUN_ID>
playbook attestation verify \
  .playbook-runs/<RUN_ID> \
  .playbook-runs/<RUN_ID>/attestation.json
```

The key is supplied by the host/environment and is never written into the run directory. Programmatic hosts may use the generic signer interface to delegate signing/verification to KMS, HSM or Vault without changing attestation payloads.

## Runtime conformance and benchmarking

Check the reference adapter contract and a concrete plan/runtime match:

```bash
playbook conformance --dry-run
playbook preflight playbooks/examples/kernel-demo.yaml --input name=Ada --dry-run
```

Benchmark deterministic workflows:

```bash
playbook benchmark playbooks/examples/kernel-demo.yaml \
  --input name=Ada \
  --dry-run \
  --approve approval \
  --approval-actor benchmark \
  --repeat 10
```

Provider-specific adapters can reuse the same conformance API and benchmark model.

## Operator and catalog

The repository includes a deterministic metadata router for offline bootstrap/testing. Production hosts can replace it with a model-backed operator while preserving typed selection and compile gates.

```bash
playbook catalog search "research decision" --root playbooks/examples
playbook catalog lint playbooks/examples
playbook route "release qa audit" --root playbooks/examples
```

Catalog entries include content hashes. The linter rejects the dangerous case where the same `id@version` names different content.

## Agent Skills integration

This repository deliberately does **not** absorb skill libraries. A playbook declares skill dependencies; a resolver locates a compatible library and records a content lock.

```bash
playbook skills index ../agent-skills
playbook compile playbooks/examples/evidence-to-decision.yaml \
  --input question='"Should we ship?"' \
  --skills-root ../agent-skills \
  --strict-skills \
  --out /tmp/plan.json

playbook skills verify-plan /tmp/plan.json ../agent-skills
```

Locks hash the full skill directory, including scripts, references and assets. Symlinks are rejected. Duplicate skill IDs across configured roots are treated as ambiguity, not resolved by root order.

## Core contract

Runtime precedence:

```text
Policy > compiled playbook > operator > skill > action/tool
```

The model is never the root authority. Mandatory approvals, budgets, dependency edges, isolation requirements, retry safety and permission constraints are evaluated by the kernel.

The principal state domains remain separate:

```text
execution state != semantic memory != evidence != artifacts != approvals
```

## Repository layout

```text
src/agent_playbook_os/   reference kernel
playbooks/examples/      executable examples
schemas/                 generated machine-readable contracts
evals/                   golden/adversarial cases
tests/                   unit, recovery and security tests
adapters/                host integration guidance
docs/architecture/       architecture and threat model
docs/specification/      execution contracts
docs/adr/                binding architectural decisions
scripts/                 validation and release tooling
```

## Release checks

```bash
make check
python scripts/release_check.py
python scripts/check_reproducible_release.py
```

The release builder honors `SOURCE_DATE_EPOCH` / `--source-date-epoch` and normalizes ZIP metadata, so two builds from identical source and epoch must produce the same archive SHA-256. The builder rejects source-tree symlinks and excludes caches/build artifacts.

A green release check proves repository/control-plane invariants, not factual correctness of model output or safety of an external provider implementation.

## Design boundaries

This project intentionally avoids:

- role-playing agent hierarchies as the core architecture;
- hidden mutable shared memory;
- unversioned prompt chains;
- LLM-controlled mandatory security gates;
- implicit remote code/schema loading;
- retrying uncertain material side effects without reconciliation/idempotency proof;
- provider SDK dependencies in the kernel;
- pretending that a declared `sandbox` exists when the host cannot provide one.

See [Architecture](docs/architecture/ARCHITECTURE.md), [Threat model](docs/architecture/THREAT_MODEL.md), [Playbook specification](docs/specification/PLAYBOOK_SPEC.md) and [ADRs](docs/adr/).

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
