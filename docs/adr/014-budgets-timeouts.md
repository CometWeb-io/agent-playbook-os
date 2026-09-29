# ADR-014: Budgets and timeouts are kernel controls

## Decision

Execution and policy may define hard ceilings for wall time, per-step time, attempts, step-class calls, model/tool calls, tokens, and cost. The stricter of execution and policy limits wins.

## Why

Budgets are safety and operations constraints. They cannot be advisory prompt text. Countable call limits are checked before the next invocation; provider-reported token/cost usage is checked immediately after the result is returned.
