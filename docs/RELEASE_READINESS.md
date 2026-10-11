# Release readiness

This checklist separates deterministic reference-runtime evidence from live
provider evidence. A release is not ready to be described as provider-ready
while any required live item remains `BLOCKED`.

## Current snapshot — 2026-09-30

| Gate | Status | Evidence / remaining work |
| --- | --- | --- |
| Fresh install gate | CLEAR | From a clean clone: `python3 -m venv .venv`, activate, then `python -m pip install -e ".[dev]"` (includes the no-isolation build backend); clean virtualenv regression passes. Package is installed from the clone, not PyPI. |
| Security regression suite | CLEAR | Command allowlist, expression, path, secret and policy tests pass. |
| Skill-lock verification | CLEAR | Resolver, whole-package hashes and Agent Skills adapter contract pass. |
| Migration test | CLEAR | Released run-state migration suite passes. |
| Reproducible source release | CLEAR | `scripts/check_reproducible_release.py` passes locally. |
| Reproducible wheel | CLEAR | `scripts/check_reproducible_wheel.py` passes locally. |
| Reference adapter smoke | CLEAR | Injected-client/reference conformance and smoke tests pass. |
| Live provider adapter smoke | BLOCKED | No live Claude/Codex/ChatGPT host adapter or sandbox evidence is shipped here. |
| Real legacy-vs-playbook comparison | BLOCKED | The first harness case is structural-only; quality, factuality and business outcome are unverified. |

## Required local release command

The required pull-request CI gate is intentionally cheap: Python 3.12,
pytest, schema export and repository validation. Before publishing a release,
run the full local gate from a clean development environment:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
python scripts/release_check.py
python benchmarks/run_comparison.py \
  --case benchmarks/cases/evidence-to-decision.yaml --dry-run
git diff --check
```

The comparison command is evidence of structural compatibility only. A real
release note may claim live provider support only after a host-specific adapter
smoke and one real golden scenario are recorded with the same skill-library and
model configuration hashes.

## Subsequent security gate (11 October 2026)

- Worker-queue `approvals` are **requests, not human authorization**; a
  host-owned `approval_authorizer` is required to grant them. The standalone
  queue CLI refuses `--approve` rather than assigning fake human authority.
- Compiled plans lock a bounded transitive graph of child playbook sources.
  The full graph is verified before creating run data; missing child locks on
  old plans require explicit recompile and review.
- Generic and tenant file-secret resolution now uses POSIX no-follow directory
  handles and fails closed on unsupported platforms.
- These controls have unit/regression evidence. Identity infrastructure,
  multi-host production storage, full live-provider adapters, real human
  authentication and comparative quality evals remain **BLOCKED**.

### Material exception contract (11 October 2026)

The reference engine now classifies unqualified exceptions after a material
dispatch as `UNKNOWN` rather than a definitively failed no-effect call.
Before releasing host adapters, verify that their `NoSideEffectPerformed`
signals are based on authoritative non-execution evidence, and that
`supports_idempotency(step)` means real provider-side deduplication with
the actual nonempty rendered key. Network errors must remain ambiguous
unless independently reconciled. The new local regression cases are not
a substitute for provider-native chaos and live idempotency tests.

### Resume retry eligibility (11 October 2026)

Before deploying a host adapter, verify that it honors durable
`retry_eligible`, never treats a provider tool-continuation request
as a retryable ordinary error, and enforces the remaining step retry
budget after a worker restart. Previously persisted records without
this optional field are not auto-retried. The reference tests verify
these guardrails locally; live idempotency and external outcome
reconciliation remain required.
