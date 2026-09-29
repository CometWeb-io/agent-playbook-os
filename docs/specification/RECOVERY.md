# Recovery and reconciliation

## Why reconciliation exists

A process can fail after an external system commits a request but before the local runner receives or persists the response. Treating that state as an ordinary failure can duplicate a payment, deployment, CRM write or other material side effect.

Agent Playbook OS distinguishes:

```text
FAILED   -> the runner has a terminal failure result
UNKNOWN  -> the runner cannot prove whether the side effect committed
```

## Automatic recovery

A material `UNKNOWN` invocation may be retried automatically only when both are true:

1. the playbook has a stable idempotency key;
2. the runtime adapter explicitly reports idempotency support for that exact step/downstream operation.

The existence of a key in YAML alone is insufficient.

## Inspect first

Use the run inspection surfaces before reconciliation:

```bash
playbook inspect <RUN_DIR>
playbook uncertain <RUN_DIR>
playbook receipts <RUN_DIR>
```

If the external provider exposes a native acknowledgement that was not persisted before the crash, attach it without claiming success:

```bash
playbook receipt attach <RUN_DIR> <INVOCATION_ID> \
  --provider example \
  --call-id job_123 \
  --status accepted \
  --actor ops
```

Receipt attachment preserves lineage. It does not complete the step.

## Manual reconciliation

Check the external system of record first. Then record one of:

### Not executed

```bash
playbook reconcile <RUN_DIR> <INVOCATION_ID> \
  --outcome not-executed \
  --actor ops
```

The step stays unfinished and may execute on resume.

### Succeeded

```bash
playbook reconcile <RUN_DIR> <INVOCATION_ID> \
  --outcome succeeded \
  --output '{"external_id":"abc"}' \
  --actor ops
```

The reconciled step becomes completed with the supplied canonical result. Reconciliation records actor, note and durable event.

## Lease loss

Heartbeat loss while waiting for a material operation is an uncertain-outcome window. The local task is interrupted, the invocation becomes `UNKNOWN`, and automatic retry is blocked unless the idempotency contract is proven.

## Migration before recovery

Old supported snapshots can be inspected in memory. Before an operational repair, persist the current schema explicitly:

```bash
playbook migrate-state <RUN_DIR> --actor ops
```

The command backs up the source snapshot before writing the migrated version.

## Failure injection

The test harness includes deterministic fault injection around execution/resume. Recovery tests must prove that completed steps/sibling branches are not repeated and that uncertain side-effect windows fail closed.
