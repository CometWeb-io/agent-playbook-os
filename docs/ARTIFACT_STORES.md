# External artifact stores

The core `ArtifactRef` already carries a URI and content hash. v0.6 adds an explicit external verification boundary instead of silently skipping non-`file:` artifacts.

## Contract

An `ArtifactStoreProtocol` provides:

- `put(scope, artifact_id, data)` -> hash-bound `ArtifactRef`;
- `get(scope, ref)`;
- `verify(scope, ref)`.

`FilesystemArtifactStore` is the reference implementation and emits:

```text
artifact+file://<tenant>/<namespace>/<filename>
```

The store verifies both tenant/namespace routing and SHA-256 content integrity.

## Fail-closed verification

A run containing external artifact URIs fails `verify`/`attest` unless a verifier for that scheme is supplied. Unknown external schemes are not treated as verified merely because the local run directory is internally consistent.

Example:

```bash
playbook verify .playbook-runs/<run> \
  --artifact-root .playbook-artifacts \
  --tenant acme --namespace prod
```

Production adapters can map the same contract to S3, GCS, Azure Blob or another content-addressable store. The host remains responsible for credentials, bucket policy, retention, encryption and object-lock configuration.
