# Security policy

Report suspected vulnerabilities privately to hello@cometweb.io before public disclosure. Do not open a public issue for an unfixed vulnerability.

Security-sensitive surfaces include expression evaluation, action execution, approval bypass, path traversal, playbook/skill/plugin supply chain, remote schema resolution, secret handling, runtime isolation, cancellation/recovery, run leases, invocation reconciliation, provider receipts, streaming cleanup, telemetry redaction, persistence/migrations, provenance/attestation, and capability escalation.

The reference runtime is intentionally conservative:

- shell strings are not executed and command actions are opt-in;
- enabling command actions requires a non-empty executable allowlist; each argv executable must match its configured absolute path or basename;
- `sandbox`/`subagent` claims fail closed when unsupported;
- sensitive inputs persist references rather than raw credentials;
- secret references resolve at the runtime boundary and kernel-owned durable surfaces redact marked secret values;
- file-secret reads are root-confined and size-limited;
- remote schemas require a SHA-256 pin and explicit resolver;
- uncertain `external`/`destructive` outcomes require reconciliation unless exact idempotent retry is explicitly supported;
- native provider receipts are preserved but never treated as automatic proof of success;
- active run ownership uses renewable TTL leases; lease loss during material work becomes outcome uncertainty;
- strict capability negotiation can reject an incompatible runtime before execution state is created;
- plugin discovery is metadata-only and does not silently authorize provider code;
- telemetry failures do not control workflow success;
- attestation keys/signing infrastructure are host-owned and never stored in the run directory.

A passing conformance suite is not a security certification of a provider adapter, sandbox, plugin or external system.

## Distributed execution boundaries (v0.6)

- Queue `work_id` is a submission idempotency key; conflicting payload reuse is rejected.
- Claim token + monotonic fence is required for heartbeat/ack/nack. A stale claimant cannot finalize newer work ownership.
- The optional run-resource fencing coordinator is independent from the queue claim and is checked again before acknowledgement.
- Workers can confine queued `plan_path` and `run_dir` to explicitly configured filesystem roots.
- Tenant/namespace policy files and tenant-secret files reject symlinks/path traversal.
- Tenant scope must come from an authenticated host/queue boundary, not from untrusted playbook instructions alone.
- External artifact URIs fail integrity verification and attestation unless the host supplies an explicit verifier for the scheme and tenant scope.
- The bundled SQLite queue/coordinator is a semantic reference for local/multi-process validation, not a production multi-host HA service.
