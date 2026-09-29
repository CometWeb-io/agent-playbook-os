# Contributing

## Development setup

```bash
python -m pip install -e ".[dev]"
make check
```

The development extra is intentionally self-contained: it includes the
`setuptools` backend and `wheel` needed by the no-build-isolation reproducible
wheel check. No manual package installation should be necessary before the
release gate.

Before a release or material control-plane change:

```bash
python scripts/release_check.py
```

The nested clean-environment regression is available when validating release
tooling locally:

```bash
AGENT_PLAYBOOK_RUN_RELEASE_TOOLING=1 python -m pytest tests/test_release_tooling.py -q
```

## Change classes

- **spec change:** update models, exported schema, specification docs, migration note and conformance tests;
- **runtime/control-flow change:** add unit + recovery tests and verify resume/event semantics;
- **policy/security change:** add an adversarial regression and update the threat model when the trust boundary changes;
- **adapter change:** keep provider SDK dependencies outside core; injected-client bridges may live in core only when importing them does not require the vendor SDK;
- **store change:** add durability, lease, migration and concurrent-ownership tests against the common store contract;
- **secret/plugin change:** add leakage/ambiguity/admission tests and document the trust boundary;
- **skill integration change:** preserve external package ownership and supply-chain lock behavior;
- **playbook example:** must compile under `scripts/validate_repo.py`;
- **eval change:** keep cases deterministic unless the suite explicitly documents a model-backed scorer.

## Architectural rules

1. Read relevant ADRs before changing the owning subsystem.
2. Prefer deterministic checks over model judgment when a condition can be encoded.
3. Do not silently degrade a declared safety or validation feature.
4. Keep provider-specific behavior behind adapters.
5. Do not use root order, prompt text, or runtime model output as an authorization mechanism.
6. A retry of a side effect is a new execution and must preserve idempotency semantics.
7. A passing structural test does not prove external factual correctness.
8. Provider receipts improve reconciliation but never substitute for system-of-record verification.
9. Capability declarations are admission metadata, not permissions or security certification.

## Pull request evidence

Include:

- problem being solved;
- owning architectural layer;
- compatibility/schema impact;
- tests/evals executed;
- failure/recovery behavior if control flow changed;
- security/policy impact;
- before/after trace or run diff when relevant.

## Release artifacts

Use:

```bash
python scripts/build_release.py --out dist/agent-playbook-os-vX.Y.Z.zip
```

The builder excludes transient caches and writes a SHA-256 release manifest into the archive.

## Distributed adapter changes

Queue, fencing and artifact-store adapters are compatibility-sensitive. New adapters should preserve the semantics documented in `docs/DISTRIBUTED_EXECUTION.md` and pass distributed conformance before being described as compatible. In particular, do not treat method-shape compatibility as proof of claim fencing, idempotent submission or tenant isolation.
