# Distributed execution contract

Distributed execution is an admission and ownership layer around an immutable `CompiledPlan`.
It does not change playbook semantics.

## Work submission

A submission binds:

- stable `work_id`;
- queue name;
- tenant + namespace;
- run ID and run location;
- exact compiled-plan path;
- run-store backend;
- mandatory capability set;
- approval set/actor;
- retry ceiling;
- metadata.

Reusing a `work_id` with the same semantic payload is idempotent. Reusing it with a different payload is an error.

## Claim

A successful claim records:

- worker identity;
- opaque claim token;
- monotonically increasing fence;
- attempt number;
- claim/heartbeat time;
- visibility timeout.

Only the current `(claim_token, fence)` pair may heartbeat, acknowledge or negatively acknowledge the item.

## Capability routing

Mandatory queue capabilities are derived from the compiled plan using the same requirement model as runtime preflight. Workers advertise concrete capability IDs and `kind:<kind>` aliases. A worker may claim only work whose mandatory set is satisfied.

## Resource fence

The reference worker may additionally acquire an independent run-resource fence. Queue ownership and resource ownership are intentionally distinct. This protects the case where a queue message is reclaimed while a stale worker still has local state.

## Completion

A worker may acknowledge success only after:

1. Runner returns successfully or an existing run is proven `COMPLETED`;
2. queue claim is still current;
3. resource fence is still current when configured.

A stale holder must fail closed.

## Failure

A failure is nacked under the current fence. Attempts below `max_attempts` return to `PENDING`; exhaustion becomes `DEAD`. Material provider uncertainty remains represented by Runner invocation state and cannot be converted into ordinary failure by the queue.
