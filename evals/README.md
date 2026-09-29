# Eval harness

The repository treats control-plane behavior as testable product behavior rather than prompt folklore.

## Executable suites

`evals/cases/kernel.jsonl` covers golden happy-path/control-flow behavior.

`evals/cases/adversarial.jsonl` is also executable and covers fail-closed behavior such as:

- policy bypass attempts still stopping at a human gate;
- expression function-call/RCE attempts failing before action execution;
- raw sensitive inputs being rejected;
- remote schemas without a SHA-256 pin being rejected.

An eval case can expect either a completed/failed run state or an intentional compile rejection via `compile_error_contains`.

Run both with:

```bash
make eval
```

## Other harness layers

`tests/` contains executable kernel, security, recovery, lease, cancellation, attestation and supply-chain tests.

`playbook conformance` checks the generic runtime contract and basic declared capabilities.

`playbook benchmark` runs a playbook repeatedly and reports timing plus usage aggregates so host/model adapters can be compared on a stable workflow.

Record/replay cassettes provide deterministic runtime-boundary reproduction; run diffing compares outputs, step statuses and usage/provenance changes.

A passing harness proves the encoded contract only. It does not certify the factual quality of model output, real sandbox strength, downstream idempotency, or external system correctness.
