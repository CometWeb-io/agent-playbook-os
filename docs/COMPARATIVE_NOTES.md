# Comparative design notes

The architecture intentionally borrows mechanisms, not product identity:

- **GitHub Spec Kit:** explicit workflows, step registry, durable run state, resume, human gates and catalogs.
- **Agent Skills ecosystem:** portable `SKILL.md` capabilities and progressive disclosure.
- **OpenAI Agents SDK/repository practices:** small execution primitives, tracing/guardrail boundaries, repo-local skills and durable maintainer references.
- **Microsoft Agent Framework / LangGraph:** workflow is distinct from agent, checkpointing and resumable execution.
- **Promptfoo-style harnesses:** repeatable eval and red-team scenarios belong in CI.

The kernel deliberately avoids crew/persona topology, provider lock-in, implicit shared memory, and free-form handoffs as the primary architecture.

## First comparison harness

`benchmarks/run_comparison.py` writes a redacted JSON record for a fixed case.
The first case, `benchmarks/cases/evidence-to-decision.yaml`, is intentionally
structural-only: it compiles the Playbook OS path and records the legacy path as
not configured. It checks that both paths use the same declared skill-library
and model hashes, but it does not claim to measure model quality, factuality or
business outcome. Live comparisons require explicit legacy and host adapters.
