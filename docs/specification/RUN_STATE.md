# Run state and provenance

A run has durable control-plane state independent of any model provider. The reference filesystem backend uses:

```text
<RUN>/
  plan.json
  state.json
  events.jsonl
  cancel.request.json      optional
  attestation.json         optional
  artifacts/
  migrations/
  subruns/
```

The SQLite backend stores plan/state/events/cancellation/lease records in `run.sqlite3` while keeping content-addressed local artifacts under the run directory. Callers should use the store protocol rather than assuming either physical layout.

## RunState v5

`RunState v5` is the durable pause/resume boundary. Important fields include:

- run identity, playbook identity, replay provenance, parent/root lineage, lineage depth and fork reason;
- `playbook_hash`, `plan_semantic_hash`, `plan_hash` and run-state `integrity_hash`;
- top-level `steps` and durable `nested_steps` keyed as `parent/branch/step`;
- invocation journal with stable invocation identity;
- provider-native `ProviderReceipt` records and links from invocation/step state;
- pending approvals and durable approval records;
- artifacts and evidence refs;
- aggregate usage/budget counters;
- cancellation reason/time;
- runtime capability fingerprint;
- active storage backend identifier.

The migration registry supports released v2 -> v3 -> v4 -> v5 transitions. Reading may migrate in memory, but `playbook migrate-state` is the explicit operation for persisting the current schema and creating a backup first. Unsupported future versions fail closed.

## Invocation journal

Each runtime invocation records:

- invocation ID and durable step identity;
- attempt number, kind and side-effect class;
- idempotency key when present;
- start/finish timestamps;
- terminal or uncertain status;
- provider/provider call ID when known;
- linked provider receipt IDs;
- result hash or error;
- reconciliation metadata.

Statuses:

```text
STARTED
UNKNOWN
SUCCEEDED
FAILED
CANCELLED
RECONCILED
```

`UNKNOWN` is materially different from `FAILED`: the kernel cannot prove whether an external/destructive side effect committed. Resume requires reconciliation unless the adapter proves idempotent retry for the exact step.

## Provider receipts

A provider receipt is evidence that a downstream system assigned an identity/status to an invocation. It may contain a provider call/message/job ID, operation, status, idempotency key and metadata.

A receipt **does not by itself prove business success**. Attaching a receipt to an uncertain invocation preserves provenance but does not automatically mark the step complete. Reconciliation must still compare the external system of record with the intended operation.

## Event log

Events form a contiguous SHA-256 chain through `prev_hash`/`event_hash`. Verification also enforces sequence numbers and a single run ID.

The chain proves internal consistency, not authorship. Attestation can bind the current plan hash, state hash, event-chain head and artifact hashes to a host-held signing boundary.

## Lease and heartbeat

Execution acquires a per-run lease before mutating a run. The lease has a TTL and heartbeat. A second runner fails with `LeaseConflict` while the lease is live. A stale lease can be reclaimed after TTL expiry according to backend semantics.

Lease loss during a material in-flight side effect is safety-sensitive: local waiting is interrupted and the invocation becomes `UNKNOWN` rather than being treated as an ordinary retryable failure.

## Runtime capability fingerprint

The run stores a stable fingerprint of the runtime capability descriptors used at start. Resume rejects a materially different capability set by default. An operator may explicitly allow the runtime change, which is emitted into the event log.

This is drift detection, not proof that an adapter truthfully implements its advertised capabilities.

## Cancellation

Cancellation is cooperative and durable. A runner observes the backend cancellation record while steps are active.

- `none/local`: cancellation is recorded as cancellation;
- `external/destructive` interrupted in-flight: invocation becomes `UNKNOWN`;
- the run becomes `CANCELLED` with reason/time.

A terminal cancelled run is not resumed implicitly.

## Resume

Resume refuses to continue when:

- event-chain integrity fails;
- stored plan integrity fails;
- state integrity fails;
- plan/state hashes disagree;
- the run is already completed/cancelled;
- a material invocation has unresolved `STARTED/UNKNOWN` outcome;
- a live lease is owned elsewhere;
- the runtime capability fingerprint changed without explicit authorization.

Completed/skipped top-level and nested steps are never intentionally re-executed.

## Replay

Replay creates a new run, may choose a different storage backend, and records the source run ID in `replayed_from`. It never mutates the source run. Strict recorded-runtime cassettes can reproduce runtime responses/streams without contacting the original provider.


## Lineage

Every replay, fork and sub-playbook run is a new run. v5 records `parent_run_id`, `root_run_id`, `lineage_depth`, `fork_reason` and `replayed_from` where applicable. Lineage is provenance only; it does not authorize mutation of the source run.
