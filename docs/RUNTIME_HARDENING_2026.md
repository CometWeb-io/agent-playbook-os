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
2. Keep transitive dependency locks compatible across schema versions. Child
   playbook sources and local JSON Schemas are SHA-256 pinned at compilation;
   production dependency distribution still needs signed catalogs and trust roots.
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

## Host-authorized queue approvals and transitive child locks (11 October 2026)

A work submission's `approvals` and `approval_actor` are **untrusted fields**.
`DistributedWorker` now rejects submitted approval requests unless a host
provides an `approval_authorizer(item, compiled_plan)` callback. The callback
must return an independently verified `(set[str], authenticated_actor)` tuple;
every requested ID must be included in the host's grant. The actor in persisted
approval records comes exclusively from this callback. Plain CLI
`queue enqueue --approve` is intentionally rejected. Without a host grant,
workers pause on human gates and emit `WAITING_APPROVAL` instead of inferring a
human decision.

`CompiledPlan.subplaybook_locks` contains portable relative paths and SHA-256
hashes of parsed child playbooks across a bounded transitive import graph.
Compiler rejects path escapes, cycles, excessive nesting and more than 64
unique child files. Runner verifies the entire locked dependency manifest
**before** run directory creation and repeats direct source verification before
every child invocation. Old compiled plans without child imports retain their
original integrity hashes. Older plans containing child steps but lacking locks
must be recompiled and reviewed; no silent upgrade of executable semantics.

This is source integrity, not a signer identity or publication-trust proof.
Signed package distribution, manifest signing, actor authentication, replay
anti-reuse and real-provider conformance remain separate product gates.

## Hardened generic file-secret resolution

The generic `FileSecretResolver` now validates the requested path against
configured roots and reads the canonical target through a POSIX directory
handle with `O_NOFOLLOW` for each directory and the final file. It verifies
regular-file identity with `fstat`, limits reads to `max_bytes + 1`,
rejects invalid UTF-8, and wraps the result in a redaction marker. This avoids
the previous `stat`-then-`read_text` race. Roots must be trusted by the host.
Non-POSIX systems currently fail closed rather than silently using an unsafe
implementation. Cross-platform secure support requires an OS-native adapter.

## Stream limits and provider identity (11 October 2026)

The provider-neutral `Runner` now defaults to bounded asynchronous streams:

| Runner option | Default | Meaning |
| --- | ---: | --- |
| `max_stream_events` | 10,000 | Absolute maximum event count in one step invocation |
| `max_stream_event_bytes` | 2,000,000 | Maximum encoded size of any single provider event |
| `max_stream_total_bytes` | 16,000,000 | Maximum cumulative encoded stream bytes per invocation |

All values must be positive integers and the per-event ceiling cannot exceed
the total ceiling. Hosts can override them for a reviewed workload. Limits are
propagated to child runners. A stream may produce progress, usage, artifacts,
receipts, evidence and logs, but **exactly one final `result` event** is
required. Events following that terminal result and streams that end without
a result fail closed. Provider async generators are closed on completion or
failure to release held resources.

Protocol/resource errors are `StreamProtocolError` (a
`ProviderContinuationRequired` subclass). They cannot be retried
blindly or swallowed by `on_fail=skip`. If a provider may have performed a
material action, its invocation remains `UNKNOWN` until authoritative
reconciliation.

`OpenAIResponsesRuntime` now includes its configured `model` in runtime
capability metadata. `AnthropicMessagesRuntime` includes both `model` and
`max_tokens`. As a result, model/configuration changes between a run and
`resume` are detected by the existing runtime fingerprint check. Continuing
with different settings still requires explicit
`allow_runtime_change=True` and a real host review. This fingerprint does
not prove that the provider SDK, endpoint, credentials or arbitrary injected
callable code remained unchanged; hosts must record and verify those identities.

Canonical integrity hashing now rejects nonfinite floating-point values and
typed runtime metrics disallow NaN/Infinity. This prevents invalid JSON
hashes and nonfinite cost counters from bypassing deterministic budget checks.

These protections are verified by local adversarial tests with injected
async streams and stubbed provider clients. They are **not** live provider
integration certification and do not enforce a remote network cut-off after
SDK thread cancellation.

## Runner instance ownership (11 October 2026)

One `Runner` instance owns mutable execution state: event sequence, approved
step IDs, cancellation token and active lease heartbeat. It **must not** be
shared across simultaneous runs. Previously, a second invocation could
overwrite this state while a first run was in flight. The reference runner
now rejects a concurrent invocation before creating its second run directory.
After completion or failure, the same instance may be used again; every
fresh run resets event sequence to 1, while `resume` reconstructs its
sequence from the persisted event log. Both paths have regression tests.

For scalable hosts, create one Runner per claimed work item. Sharing
stateless dependencies (e.g. a properly thread-safe runtime adapter or
read-only catalog) is separate from sharing a mutable Runner instance.
These guards prevent accidental in-process state contamination; they do
not substitute for fencing and leases across multiple processes or hosts.

## Self-contained output JSON Schemas (11 October 2026)

Output-schema validation runs against the **pinned bytes** provided by the
compiler or explicitly configured host schema resolver. It must not retrieve
unrelated resources from a `$ref`, `$dynamicRef`, or `$recursiveRef`
inside those bytes, including HTTP(S), `file://`, relative files or
service-local addresses. The kernel rejects nonfragment references, constructs
an explicit `referencing.Registry` whose `retrieve` callback fails closed,
and permits internal JSON Pointers or anchors such as `#/$defs/item` and
`#item`.

Schema size is limited to **2,000,000 bytes**, with structural bounds of
**20,000 nodes** and nesting depth **80**. This protects against untrusted
schema inflation and limits the reach of registry/resolution. Runtime output
validation still uses the declared JSON Schema semantics; it does not
transform or auto-correct the output. Hosts that need external dependencies
must fetch, authorize, pin and bundle those resources independently before
executing a playbook. Runtime implicit network retrieval remains prohibited.

This hardening does not make arbitrarily complex schemas safe from high CPU
cost by itself. Production hosts should additionally enforce process-level
resource limits and reject overly complex schemas during review.

## Bounded, hash-linked record/replay cassettes (11 October 2026)

The reference `RecordingRuntime` writes a JSONL cassette with
`prev_hash` and `record_hash` on each record, using canonical SHA-256 over
the redacted body. Append writers verify the terminal record and serialize
writes with POSIX file locks when available. A failed cassette commit after a
provider invocation is treated as `SideEffectOutcomeUnknown`, not as proof
the downstream operation failed.

Default limits are **128,000,000 bytes per cassette**, **24,000,000 bytes
per record**, **10,000 streaming events per invocation**, and **16,000,000
serialized stream bytes per invocation**. Replay enforces cassette, record
and record-count limits before loading; it validates the complete record
chain before execution. A replay divergence does not consume the record.

Legacy cassettes without digests require explicit
`ReplayRuntime(path, allow_legacy=True)`. Mixing digested and legacy records
is rejected, and appending new digested records to an old cassette is refused.
Start a new cassette when upgrading. The unsupported-object fallback no longer
persists raw `repr()` text, which could contain credentials.

**Authenticated mode:** for evidence that must resist unauthorized
rewrites, configure the SDK with the same separately managed signing key:
`RecordingRuntime(inner, path, integrity_key=key)` and
`ReplayRuntime(path, integrity_key=key)`. A key must be at least 32 bytes.
Each record is then authenticated with HMAC-SHA256 over its canonical digest.
Wrong or missing keys, unauthenticated append attempts and attempts to
downgrade to legacy records are rejected.

**Important limitation:** plain SHA-256 chains without this optional HMAC
provide accidental-corruption and inconsistency detection, **not authenticity**.
HMAC protects only while its key is kept outside the cassette and remains
uncompromised. High-assurance evidence also needs independent key custody,
rotation, an external trust anchor, anti-rollback/version checkpoints, and
retention controls. Avoid storing raw credentials or PII; telemetry redaction
is best effort.


## Signed approval grants for queued workers

`HMACApprovalAuthorizer` provides a host-issued and worker-verified
authorization primitive. A signed v1 grant binds `audience`, tenant,
namespace, queue, work/run IDs, exact plan hash, plan and run destinations,
backend, max attempts, approved step IDs, actor, issuer, nonce and expiration.

The CLI only accepts signed grants with explicit work and run IDs; workers
check HMAC using a separately configured environment key. Unsigned
`--approve` remains refused for queue submissions. Grant payloads are
redacted from CLI output and excluded from persisted run metadata; a
metadata digest remains for provenance.

The reference HMAC mechanism does **not** implement user authentication,
revocation, one-time nonce consumption or authorization policy. Hosts must
authenticate/authorize human sessions before signing; retries of the same
work ID can reuse its unexpired grant. External IAM, key custody and
anti-replay ledger remain release gates. See
[signed approval grants](SIGNED_APPROVAL_GRANTS.md).

## Secret-safe cassette input identity

Ordinary unkeyed recording signatures historically hashed only redacted
inputs. Different secrets such as `SecretValue("a")` and `SecretValue("b")`
both became the same placeholder; replay could therefore match the wrong
invocation. Plain `password`, `api_key` and Bearer-header strings had the
same collision problem whenever redaction removed information.

The recorder now **rejects any lossily redacted input before dispatch** unless
a secret `integrity_key` of at least 32 bytes is configured. In keyed mode,
the invocation signature is HMAC-SHA256 over a domain-separated canonical
input that includes actual secret bytes and the `SecretValue.source_ref`.
Only the HMAC fingerprint and redacted inputs are persisted — never the
original values. Replay requires the same key, and different secrets,
references or ordinary password values no longer match.

Older recordings with no `signature_scheme` retain compatibility only
for inputs whose redaction is lossless; legacy replay involving a secret
is refused because its input identity cannot be recovered safely.

The standard CLI can opt into keyed cassettes using
`--record-cassette path --cassette-key-env APBOS_CASSETTE_KEY` or
`--replay-cassette path --cassette-key-env APBOS_CASSETTE_KEY`.
The key must exist in the worker's environment and **must not** be stored
with the cassette or playbook. SDK users pass `integrity_key` directly
to `RecordingRuntime`/`ReplayRuntime`.

Limits: this only detects values redacted by the built-in markers/rules.
An unmarked plaintext secret in an ordinary field may still be recorded
by custom adapters. Hosts must enforce secret classification, key custody,
output-data minimization and log/retention rules.

## Operator reconciliation, leases and evidence consistency (11 October 2026)

Manual `receipt attach` and `reconcile` operations now **acquire a
separate per-run lease before reading state**. They refuse to mutate a run
already owned by an active Runner or another operator. While holding that
lease they verify the event hash chain, compiled plan integrity and state
snapshot integrity, preventing stale or corrupted evidence from being
silently reconciled.

For SQLite stores, operator reconciliation and receipt attachment use
`append_event_with_state`: one database transaction commits both the
new state snapshot and matching audit event. Fault-injection tests abort
an event insert and assert that neither the state nor event journal
changed. Filesystem state/event files remain separate durable writes,
protected by the lease but **not transactionally atomic**; after a crash,
recovery must verify both before resuming material operations.

Receipts with the same `receipt_id` cannot silently claim a different
provider call or payload. Failed collision checks do not append audit
events. Reconciliation notes, outputs and CLI-returned receipt metadata
are redacted before durable persistence or display; these controls do
not authenticate the human operator or validate the downstream outcome.

Important: `reconcile --outcome succeeded` is a manual assertion. It
requires an operator with independent proof from the downstream provider.
The reference kernel checks integrity and mutual exclusion, but does
not independently prove that an external payment, email or other
material operation succeeded. Production operator endpoints must have
host-owned IAM, separation of duties and audited approval records.
