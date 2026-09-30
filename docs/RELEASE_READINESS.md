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
