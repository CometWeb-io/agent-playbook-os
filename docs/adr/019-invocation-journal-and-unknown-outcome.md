# ADR-019: Invocation journal and UNKNOWN material outcomes

## Decision

Persist one `InvocationRecord` per runtime attempt. Distinguish `FAILED` from `UNKNOWN` for external/destructive side effects.

## Rationale

A timeout or process cancellation does not prove that an external system rejected the request. Retrying an unknown payment/deploy/write can duplicate the effect.

## Consequence

Resume fails closed until reconciliation, unless a stable idempotency key exists and the runtime adapter explicitly proves support for it.
