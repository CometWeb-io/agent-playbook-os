# Supply-chain boundaries

## Skills

Resolved Agent Skills are locked by the content hash of the complete package tree, including scripts, references and assets. Symlinks are rejected. Duplicate IDs across roots are ambiguity errors.

## Playbooks

Compilation records source, semantic and exact compiled-plan identities. Execution uses the compiled plan rather than mutable source YAML.

## Output schemas

Remote schemas are untrusted mutable dependencies unless pinned. Since v0.4, remote schemas require SHA-256 pins and an explicit resolver adapter for remote schema URLs. Local schemas may also be pinned.

## Runtime adapters

Adapters are executable trust boundaries. Their capability claims for isolation and idempotency affect safety decisions and must be tested through conformance and provider-specific integration tests.

## Secrets

Sensitive playbook inputs are references, not raw values. Actual secret resolution belongs to the host/secret manager integration and should avoid persistence in playbook/run artifacts.

## Run authenticity

Plan/state/event hashes provide integrity. Optional HMAC attestation binds the verified snapshot to an external secret, protecting against an attacker who can rewrite and recompute all local hashes.


## Planner and promotion provenance

Model planner requests/responses are content-hashed and size-bounded. Generated playbooks are persisted as review artifacts before execution. Candidate promotion observations are hash-chained; promotion receipts bind to the registry head. This makes mutation detectable but does not turn model output or repeated success into independent proof of quality.
