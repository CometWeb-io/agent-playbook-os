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
15. capability/plugin discovery must not silently authorize execution code;
16. expressions may inspect only plain data mappings/sequences, never Python object attributes;
17. host-declared side effects are a minimum that playbooks cannot downgrade;
18. uncertain material invocations and lost leases must never be masked by on_fail;
19. child-run policies must inherit parent constraints, including during resume;
20. review and execute exact immutable plan hashes; do not silently replan on approval;
21. recording and replay must redact before JSON serialization without corrupting typed usage;
22. worker recovery must verify tenant, submission, plan, state and event-chain identity.
23. never trust queue-supplied approval strings or actor claims; require host authority;
24. pin all transitive child playbook content before plan approval and verify before dispatch;
25. preserve compiled-plan integrity compatibility when introducing optional manifests.

Run before declaring success:

```bash
python scripts/release_check.py
```

Do not claim that passing structural tests proves factual correctness of external model, tool, evidence, or skill outputs.

26. bound each provider stream's event count and encoded payload size; require a single terminal result;
27. never treat a rejected/incomplete stream with material side effects as safe to retry;
28. fingerprint model configuration so resumed executions cannot silently switch models;
29. reject nonfinite cost/usage/state values before durable hashing and budgeting.

30. human approval strings from queue inputs are not grants; use host-verified, scope-bound signatures;
31. never copy signed bearer grants or untrusted queue metadata into durable run outputs or logs;
32. approval signatures bind destination, work, tenant, exact plan hash and bounded validity window;
33. distinguish issuer authentication from actual human authentication and one-time authorization.

34. never fingerprint only redacted sensitive cassette inputs; bind them with keyed secret-aware HMAC;
35. legacy replay with secrets must fail closed, even if the cassette body itself is authenticated;
36. never persist raw secret input values, key material or unbounded repr() strings in recordings.

37. manual operator reconciliation must acquire the same run lease as the Runner;
38. SQLite operator mutation must commit state and audit event atomically;
39. reject conflicting receipt identities and redact reconciliation notes/CLI results;
40. do not infer real-world external success from a manually reconciled state alone.

41. receipt, artifact and evidence IDs are immutable within a run; conflicting duplicates fail closed;
42. if conflicting evidence arrives after material dispatch, journal UNKNOWN rather than retrying automatically;
43. provider-native receipt status transitions require versioned IDs or an explicitly governed update contract.

44. a SUCCEEDED material invocation without a completed step checkpoint is unsafe to redispatch;
45. manual reconciliation of SUCCEEDED evidence must match its original result digest;
46. never let on_fail skip/continue suppress post-dispatch material schema failures;
47. resumed completed outputs (including nested) must still satisfy pinned schemas.

48. never interpret ordinary post-dispatch material exceptions as proof that no side effect occurred;
49. NoSideEffectPerformed requires host-side authoritative proof; never infer it from HTTP/network exception type;
50. idempotent retries of unknown material outcomes require a NONEMPTY resolved key and an adapter that proves deduplication;
51. avoid persisting raw provider exception text in material failure events and operator logs.
