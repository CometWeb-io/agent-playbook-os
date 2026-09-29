# Streaming and provider receipts

## Streaming lifecycle

Runtime methods may return an async stream of typed `RuntimeStreamEvent` records:

```text
progress
usage
artifact
evidence
receipt
log
result       terminal
```

The engine aggregates durable fields and requires a terminal `result`. A stream that ends without a terminal result fails rather than silently treating partial output as success.

Timeout, cancellation and lease-loss checks apply while consuming the stream.

## Record/replay

Streaming cassettes preserve the ordered event sequence. Signatures use redacted inputs so secrets are not serialized into replay fixtures. Signature mismatch/order mismatch fails closed.

## Provider receipts

Receipts capture native provider identity such as request/message/job IDs. They improve reconciliation, audit and correlation with provider logs.

Receipts are stored in `RunState.provider_receipts` and linked to both invocation and step state. Metadata passes through redaction before persistence.

A manually attached receipt is an evidence/provenance operation, not a success transition:

```bash
playbook receipt attach <RUN_DIR> <INVOCATION_ID> \
  --provider openai \
  --call-id resp_123 \
  --operation responses.create
```

Use `playbook reconcile` separately when the external system of record proves the final outcome.
