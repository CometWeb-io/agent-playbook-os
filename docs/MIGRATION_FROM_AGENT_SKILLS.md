# Migration path from CometWeb agent-skills orchestration

The existing `CometWeb-io/agent-skills` repository remains the canonical capability library.

## Keep in agent-skills

- `SKILL.md`, references, scripts and fixtures;
- domain-specific validators;
- skill-specific evals;
- trigger metadata;
- CW-AIP domain schemas such as EvidenceEnvelope, DecisionEnvelope and ReleaseEnvelope.

## Move or re-express in Agent Playbook OS

- cross-skill workflow archetypes;
- generic sequencing and dependency rules;
- persisted workflow state;
- approval/gate mechanics;
- provider-neutral orchestration;
- cross-skill replay and control-plane evals.

## Compatibility strategy

`skill-orchestrator` should initially remain unchanged. A later compatibility release can detect Agent Playbook OS and map existing archetypes to versioned playbooks. Until behavior parity is proven, retain the legacy execution path.

Suggested first migrations:

1. `evidence-researcher -> ai-council`;
2. `web-app-auditor -> release-readiness`;
3. `cometweb-context -> product-operator`;
4. the 14-day CometWeb planning loop with specialist re-entry.

Do not perform a big-bang migration. Compare both paths with golden scenarios, traces, token/tool budgets and final-output quality.

The first native integration point is the optional
`adapters/agent_skills/AgentSkillsRuntime` boundary. It consumes a compiled
skill lock and an injected host; it does not move or duplicate the skills, and
it does not imply that a provider-specific host adapter already exists.
