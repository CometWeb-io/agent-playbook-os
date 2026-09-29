# Build Report — Agent Playbook OS 0.6.0

Release candidate date: **2026-09-30**

## Scope

v0.6 adds a distributed execution profile without moving workflow authority out of the Runner. The release introduces idempotent work submission, capability-aware queue admission, stale-claim recovery, monotonically increasing fencing, concurrent workers, tenant/namespace scoping, tenant-confined secrets and external artifact verification.

The reference distributed backend uses SQLite deliberately as an executable semantic specification. It is not presented as a production multi-region queue or coordination service.

## Verification results

- Python test suite: **273 passed, 1 skipped** (the nested clean-venv wheel check stays opt-in).
- Exported JSON Schemas: **32**.
- Bundled example playbooks: **7/7 compile**.
- Kernel golden evals: **5/5 passed**.
- Adversarial evals: **5/5 passed**.
- Runtime conformance: **PASS**.
- Distributed queue conformance: **PASS**.
- Standard smoke: **PASS**.
- Model-backed Operator smoke: **PASS**.
- Distributed queue/worker/fencing smoke: **PASS**.
- Repository validation: **PASS**.
- `compileall` for `src`, `scripts`, `tests`: **PASS**.
- Reproducible source ZIP check: **PASS**.
- Reproducible wheel check: **PASS**.
- Wheel-installed CLI `doctor`: **PASS**.
- Wheel-installed distributed queue conformance: **PASS**.

## Distributed safety properties exercised

1. A repeated `work_id` with an identical semantic payload is idempotent.
2. Conflicting reuse of the same `work_id` is rejected.
3. Workers cannot claim items whose mandatory capabilities they do not satisfy.
4. Expired claims are reclaimed with a higher fence.
5. Stale holders cannot acknowledge work after a newer fence exists.
6. Independent run-resource fencing rejects stale owners.
7. Queue attempts are bounded and exhaust into `DEAD`.
8. Workers can recover an already-completed run without repeating execution.
9. Queued plan/run paths can be confined to explicit roots.
10. Tenant policy inheritance is monotonic and rejects symlink escapes.
11. `tenant-secret://` rejects traversal and symlinked secret files.
12. External artifact references fail verification without an explicit verifier.
13. Tenant/namespace mismatch and external artifact tampering are detected.

## Artifact reproducibility

The release builder normalizes ZIP metadata under `SOURCE_DATE_EPOCH`. Two source archive builds from the same tree and epoch produced an identical digest during the gate. The Python wheel also reproduced bit-for-bit under its reproducibility check.

Reference wheel digest from the final source tree:

`b8e123126cfecc056916abf7498c2a63ba5271297fabe3fd0c35199eecff236b`

## Environment note

The clean-wheel smoke uses the locally available dependency layer for Pydantic, PyYAML and jsonschema while installing the built `agent-playbook-os` wheel itself into a fresh venv with `--no-deps`. No network fetch is needed to validate the package artifact in this environment.

## Release boundary

Passing this gate establishes the behavior of the reference implementation and its conformance suites. It does **not** certify an arbitrary external queue, database, artifact store, sandbox, identity provider or cloud IAM configuration. Production adapters must preserve the same semantics and carry their own infrastructure/security evidence.

## Hardening snapshot

- command actions now fail closed unless a non-empty executable allowlist is configured;
- the native `adapters.agent_skills` boundary verifies locked skill content before delegation and preserves host `RuntimeResult`/`unknown` outcomes;
- `benchmarks/run_comparison.py` emits a redacted structural record and refuses mismatched skill-library or model configuration hashes;
- live provider adapter smoke and real quality comparison remain explicitly blocked, rather than inferred from reference-runtime tests.
