# Distributed execution

v0.6 adds a reference distributed-execution profile without moving queue semantics into the core Runner.

## Control boundaries

```text
CompiledPlan
    |
    v
WorkSubmission --idempotent--> WorkQueue
    |                              |
    |                    capability-aware claim
    |                              v
    +--------------------------> Worker
                                   |
                              fencing lease
                                   |
                                   v
                                Runner
                                   |
                             durable RunStore
                                   |
                          ack / nack under fence
```

The queue decides **who may attempt work**. The Runner still owns playbook semantics, approvals, policy enforcement, invocation journaling, recovery and run integrity.

## Reference queue guarantees

`SQLiteWorkQueue` is an executable semantic reference, not a claim that SQLite is a multi-region queue.

It implements:

- stable `work_id` submission idempotency;
- conflicting reuse of a `work_id` fails closed;
- tenant / namespace / queue filtering in the SQL query before `LIMIT`;
- capability-aware admission;
- visibility timeout and stale-claim reclamation;
- monotonically increasing claim fence;
- stale claimant cannot `ack`, `nack` or heartbeat after re-claim;
- bounded attempts and `DEAD` terminal state;
- cancellation for pending and claimed work;
- queue statistics;
- semantic conformance probes.

External queue adapters should pass the same conformance suite before production use.

## Fencing

`SQLiteFencingCoordinator` adds a second, run-resource fence independent of the queue claim.

A worker acquires a resource such as:

```text
<tenant>/<namespace>/run/<run_id>
```

Each new acquisition increments a monotonically increasing fence. A stale holder cannot renew, assert or release a newer lease. The worker re-checks the resource fence immediately before queue acknowledgement.

Fencing does not magically make an arbitrary external side effect transactional. Provider receipts, idempotency keys and reconciliation remain required for material external/destructive calls.

## Worker recovery

A worker receiving a claimed item:

1. checks plan/run path confinement when configured;
2. verifies mandatory runtime capabilities;
3. re-applies current tenant/host policy;
4. acquires the run-resource fence;
5. opens the configured run store;
6. starts a fresh run or resumes an existing non-terminal run;
7. treats an already-completed run as recovered success without re-execution;
8. heartbeats both queue claim and fencing lease;
9. acknowledges only while still current;
10. nacks with bounded retry on failure.

Queue/fence loss is delivered into Runner cancellation. Material side effects retain the existing `UNKNOWN -> reconcile` semantics.

## CLI

Compile once, submit the immutable plan, then let a worker execute it:

```bash
playbook compile playbooks/examples/kernel-demo.yaml \
  --input name=Ada --out /tmp/demo-plan.json

playbook queue enqueue /tmp/demo-plan.json \
  --queue-db .playbook-os/work.sqlite3 \
  --tenant acme --namespace prod \
  --run-root .playbook-runs

playbook worker once \
  --queue-db .playbook-os/work.sqlite3 \
  --queue-name default \
  --allowed-run-root .playbook-runs \
  --allowed-plan-root /tmp \
  --strict-capabilities
```

A queue/work item status of `COMPLETED` means the worker finished its claim and
acknowledged the item. The underlying **run** may still be `WAITING_APPROVAL`
(for example `kernel-demo` without `--approve`). Inspect the run directory and
resume with `playbook resume` when a human gate is pending; do not treat worker
`COMPLETED` alone as end-to-end playbook completion.

Inspect operations:

```bash
playbook queue list --queue-db .playbook-os/work.sqlite3 --tenant acme --namespace prod
playbook queue stats --queue-db .playbook-os/work.sqlite3
playbook queue conformance
```

## Production adapter requirements

A production distributed backend should additionally provide:

- a network database/queue with transactional claim semantics;
- server-side clocks or a documented clock model;
- fencing tokens enforced at every shared mutable state write;
- HA artifact storage;
- tenant-aware credentials and authorization;
- encrypted transport/storage;
- operational metrics and dead-letter handling;
- backup/restore and disaster-recovery procedures.

The reference SQLite queue/coordinator exists to define and test semantics, not to replace those systems.
