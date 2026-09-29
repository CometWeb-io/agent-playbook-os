# OpenAI adapter boundary

The core package ships a thin `OpenAIResponsesRuntime` for an **injected** client exposing `responses.create`; it does not require the OpenAI SDK.

For richer OpenAI Agents SDK integrations, keep kernel gates/policies outside model instructions and map bounded `agent` steps to the provider runtime. Advertise `subagent`/sandbox only when the embedding host provides a real isolation primitive.

Preserve a native response/tool/job ID as a provider receipt only when OpenAI actually returns it. Do not treat a model handoff, receipt or response ID as authorization for an external side effect, and do not claim provider idempotency unless the downstream request really uses a supported idempotency mechanism.
