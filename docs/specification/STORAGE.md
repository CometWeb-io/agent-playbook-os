# Durable storage contract

Execution depends on a store contract rather than concrete JSON files.

## Required behavior

A store must provide durable operations for:

- compiled plan save/load and integrity verification;
- state save/load, source schema inspection and backup;
- append/read/verify event chain;
- cancellation request lifecycle;
- lease acquire/renew/status/release/break;
- provider receipt attachment;
- invocation reconciliation;
- artifact write/verification.

Snapshots must not be reported durable before the backend's durability boundary succeeds.

## Filesystem backend

The reference filesystem backend prioritizes inspectability:

- atomic replacement for plan/state;
- append + fsync event log;
- TTL/heartbeat lease file;
- cancellation marker;
- artifacts under `artifacts/`.

It is suitable for local/single-host execution and shared filesystems only when their atomicity/locking semantics are understood.

## SQLite backend

`SQLiteRunStore` keeps plan/state/events/cancellation/lease metadata in `run.sqlite3` with WAL and `synchronous=FULL`. Artifacts remain files under the same run directory.

Use:

```bash
playbook run playbooks/examples/kernel-demo.yaml \
  --input name=Ada \
  --store sqlite
```

Operational commands auto-detect SQLite from the run directory.

SQLite provides a stronger local transaction boundary; it is not a multi-region coordination service.

## Store migration

Storage backend and state schema are separate dimensions. State migration uses the same migration registry regardless of backend. Replay may create a new run on a different backend; in-place backend conversion is intentionally not implicit.

## Future backends

PostgreSQL/managed stores should implement the same protocol and add backend-specific fencing/transaction tests. A plugin may expose a store implementation through `agent_playbook_os.stores` without changing the kernel.
