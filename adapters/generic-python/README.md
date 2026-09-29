# Generic Python adapter

Use `CallableRuntime` or implement `ExecutionRuntime` and inject it into `Runner`.

Keep credentials/tool clients outside the compiled plan. Propagate the kernel invocation ID for correlation, preserve downstream native receipt IDs, and use the kernel-supplied idempotency key only with services that really honor it. Streaming handlers may return typed runtime-event async iterators.
