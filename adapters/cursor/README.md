# Cursor host adapter contract

`CursorHostRuntime` is an explicit injection boundary: the embedding Cursor/IDE integration supplies skill/action/agent handlers and declares the isolation/capabilities it can actually provide.

The repository deliberately does not shell out to undocumented Cursor commands or pretend there is a stable public Python agent SDK. Repository policies, approvals, budgets and provenance remain kernel-owned.
