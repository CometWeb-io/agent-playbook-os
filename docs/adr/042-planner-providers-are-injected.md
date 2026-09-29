# ADR-042: Planner provider clients are injected

**Status:** Accepted

Core defines a planner protocol and typed response but imports no provider SDK. OpenAI/Anthropic bridges accept caller-owned clients, preserving provider neutrality and allowing host-specific authentication, retries and telemetry.
