# Roadmap

## v0.6 — distributed execution profile (current)

Delivered:

- idempotent typed work submission and transactional SQLite queue reference;
- capability-aware claim routing derived from compiled plans;
- visibility timeout, stale-claim reclaim, bounded retry and DEAD terminal state;
- monotonically increasing queue fences and independent run-resource fencing coordinator;
- concurrent worker execution with claim/fence heartbeat and recovery of existing runs;
- tenant/namespace-scoped run paths, policy overlays and tenant secret resolver;
- external artifact store/verifier boundary with fail-closed verification;
- distributed queue conformance suite and operational queue/worker CLI;
- path-confinement controls for queued plan and run locations.
- structural legacy-vs-playbook comparison harness with redacted records and
  explicit configuration matching.

Next candidate themes:

- production Postgres/managed-queue adapters validated by distributed conformance;
- remote object-store adapters and signed artifact manifests;
- authenticated worker identities / policy-as-code integration;
- scheduler/cron/event ingress and autoscaling worker profiles.

## v0.5 — adaptive operator and bounded learning loop

Delivered:

- typed model-backed Operator with provider-neutral planner protocol;
- existing-playbook selection, ephemeral playbook proposal and explicit decline mode;
- planner request/response content hashes and response/context size ceilings;
- injected-client OpenAI/Anthropic planner bridges and generic callable planner;
- ephemeral `validate -> compile -> host policy -> capability preflight -> review -> plan approval -> execute` lifecycle;
- reviewed compiled-plan execution without re-planning;
- monotonic host/org policy overlay that a playbook/model cannot weaken;
- `RunState v5` lineage for replays, forks and subruns;
- differential replay/model lab;
- hash-chained candidate observation registry and evidence-gated promotion;
- durable bounded `foreach` fan-out;
- durable bounded `while` refinement loops with mandatory iteration ceilings;
- golden/adversarial eval coverage for bounded loops and generated-plan safety.

## v0.6 — distributed execution profile

Target work:

- PostgreSQL/managed-store reference plugin with fencing-token semantics;
- worker claim/heartbeat protocol for multi-host execution;
- durable queue integration boundary;
- remote artifact-store interface;
- resumable provider-native jobs/webhooks where supported;
- explicit tenancy namespace and per-tenant policy/credential boundaries;
- normalized provider error taxonomy and reconnect/partial-stream fault injection;
- OpenTelemetry semantic conventions for run/step/invocation/planner spans;
- standalone plugin-package examples outside core.

## v0.7 — fleet and governance profile

Candidate work after distributed-store semantics are proven:

- signed organization playbook catalogs and trust roots;
- policy bundles with versioned rollout and compatibility checks;
- worker/runtime admission registry;
- centralized run index without centralizing secret material;
- promotion review queues and organization-level approval policy;
- retention/redaction policies for durable run data;
- cross-run portfolio metrics and SLOs.

## v1.0 gate

Do not call the project 1.0 until:

- persistence/resume compatibility policy is stable across several released schemas;
- at least two real provider adapter packages pass the same conformance suite against live provider sandboxes/test accounts;
- material side-effect receipt/reconciliation semantics are battle-tested;
- isolation claims are verified per adapter;
- at least one distributed store profile proves fencing/concurrent-worker safety;
- upgrade/migration tests cover all supported released state schemas;
- generated-plan approval and promotion workflows have real-world evidence, not only fixtures;
- release archives are independently reproducible/verifiable enough for OSS consumption;
- public API and playbook schema changes follow an explicit compatibility/deprecation policy.
