# Skill integration

Agent Playbook OS consumes external Agent Skills; it does not redefine the skill package format.

## Discovery

The reference `SkillResolver` indexes:

- standard directories containing `SKILL.md`;
- repositories with a `skills/` directory;
- CometWeb-compatible `registry/skills.json` metadata.

Duplicate skill IDs across configured roots are rejected as ambiguous.

## Locking

For a locally resolved package, the compiler records:

- skill ID;
- declared source;
- version/ref when available;
- local resolution path (exact compiled-artifact metadata only);
- whole-package SHA-256 content hash;
- resolved status.

The package hash commits to relative file paths and bytes for instructions, scripts, references, assets and other package files. VCS/cache metadata is excluded. Symlinks are rejected.

The plan's portable `semantic_hash` excludes local resolution paths but includes supply-chain content hashes.

Declared `version` is an exact SemVer (including prerelease/build labels), not a
range. It must match the package's `VERSION` file; registry metadata alone is
not proof. Missing or conflicting package versions fail resolution.

A declared `ref` must be a full lowercase Git commit ID matching local `HEAD`.
The checkout must be clean, and every packaged file must match tracked HEAD
bytes. Branch/tag names, abbreviated IDs, ignored extra package files and index
flags hiding changed/deleted files are not accepted. Verification never fetches,
runs Git hooks or applies content filters. Git is needed only for ref checks.
Checkouts with configured Git clean/process filters are rejected before any
working-tree status check, because even `git status` can execute those programs.
Checks repeat when verifying a lock; they do not create an immutable snapshot
or sandbox the package. A host must still prevent changes during execution.

`optional: true` permits absence, not an ambiguous or mismatched resolved
dependency. Such errors remain errors. Existing locks whose version was only
registry metadata need a real matching `VERSION` before they can be verified.

## Strict production mode

Compile with a configured resolver and strict resolution, then enable:

```yaml
policy:
  require_resolved_skills: true
```

This prevents a production playbook from claiming a dependency that was not actually resolved/locked.

Verify an existing compiled plan against current packages:

```bash
playbook skills verify-plan plan.json /path/to/skill-library
```

## Runtime

The kernel passes the compiled step and typed input/context to an `ExecutionRuntime`. The host adapter is responsible for loading/invoking the actual skill while honoring the locked package identity and provider permissions.

The installable boundary in `agent_playbook_os.adapters.agent_skills` provides an
`AgentSkillsRuntime` wrapper for this purpose. It verifies the compiled lock at
invocation time and forwards a `SkillInvocation` with the package hash and
control-plane invocation ID. It does not select or start Claude, Codex,
ChatGPT or another provider; the injected `AgentSkillsHost` remains the owner
of that operation. `RuntimeResult` and plain payloads are returned unchanged.
To report an uncertain side effect, the host must raise
`SideEffectOutcomeUnknown`; the invocation journal retains `UNKNOWN` and resume
requires reconciliation. A payload such as `{"status": "unknown"}` is domain
data, not a control-plane signal, and must not be used to report execution
uncertainty.

The locked local path must match the resolver's verified package location.
Relocation requires recompiling/relocking: a copied package must not cause
verification of one directory followed by dispatch from an old directory.

```python
from agent_playbook_os.adapters.agent_skills import AgentSkillsRuntime

runtime = AgentSkillsRuntime(host=host, resolver=resolver, skill_locks=plan.skill_lock)
# Use Runner(runtime, enforce_capabilities=True) for admission before execution.
```

The former checkout import `adapters.agent_skills.contract` is a compatibility
shim, not an independent adapter implementation. The installed wheel includes
the canonical module. Live mode declares a skill runtime but does not invent
subagent or sandbox capabilities. Dry-run records the invocation without
calling the host. Provider-specific execution still requires an injected host.

The `evidence-to-decision` example consumes Evidence Graph v2 through
`research_status`, validates its handoff shape before the condition gate, and
proceeds only for `READY`. Its schema checks structure, not truth: the evidence
skill and consuming decision workflow still own evidence sufficiency. An
unsupported output version fails at the producer boundary rather than breaking
later on a missing gate field.

A lock proves identity, not quality. Skill behavior still requires review/evals.
