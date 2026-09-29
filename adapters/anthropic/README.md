# Anthropic Messages adapter bridge

`AnthropicMessagesRuntime` accepts an injected client exposing `messages.create`. Core does not import the Anthropic SDK.

The bridge normalizes text output and token usage and persists a provider receipt only when the response contains a real message ID. It does not claim downstream idempotency or a sandbox boundary by default.

Production integrations should add provider-specific tests for cancellation, streaming, tool use, rate-limit/error normalization and credential scoping before advertising those capabilities.
