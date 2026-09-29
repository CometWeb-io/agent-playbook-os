# Agent Skills host adapter

This is the native boundary between Agent Playbook OS and the external
`agent-skills` capability library.

`AgentSkillsRuntime` verifies the compiled `SkillLockEntry` against a supplied
`SkillResolver`, then forwards a `SkillInvocation` containing the locked skill
ID, absolute package path, whole-package content hash, Playbook OS invocation
ID, idempotency key, side-effect class, inputs and control context.

The injected `AgentSkillsHost` owns the actual host operation. It may call a
local Codex/Claude integration, a ChatGPT-managed host, or another adapter, but
that implementation is deliberately outside this repository. The boundary
returns `RuntimeResult` unchanged and also preserves a host's explicit
`unknown` outcome so the engine can apply its reconciliation rules.

The adapter does not claim provider support, sandbox isolation, idempotency or
successful side effects merely because a host is injected. Those capabilities
must be declared and tested by the host integration.
