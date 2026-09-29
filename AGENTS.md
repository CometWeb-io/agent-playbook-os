# Instructions for coding agents

Treat `docs/adr/` as binding architecture unless a new ADR explicitly supersedes a decision.

Before modifying the kernel:

1. identify the owning layer;
2. preserve provider neutrality in `src/agent_playbook_os`;
3. do not move domain skill logic into the runtime;
4. add/update tests for every control-flow, persistence or policy change;
5. preserve resume semantics, approval integrity and event provenance;
6. preserve the distinction between portable `semantic_hash` and exact `integrity_hash`;
7. never replace a deterministic gate with model judgment without an ADR;
8. never silently skip declared validation because an adapter is unavailable;
9. preserve full-package skill locking and ambiguity rejection;
10. treat side-effect retries/resume as safety-sensitive execution paths;
11. treat `RunState` schema changes as compatibility changes and register explicit migrations;
12. never assume filesystem layout in runner logic — depend on the durable store contract;
13. resolve credentials only through secret-resolver boundaries and preserve redaction;
14. provider receipts are provenance, not proof of successful business outcome;
15. capability/plugin discovery must not silently authorize execution code.

Run before declaring success:

```bash
python scripts/release_check.py
```

Do not claim that passing structural tests proves factual correctness of external model, tool, evidence, or skill outputs.
