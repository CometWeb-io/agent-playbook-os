# Replay Lab and run lineage

Replay Lab compares the same compiled semantic plan across runtime/model variants without turning benchmark output into evidence of business correctness.

For each variant it records repeated samples with:

- run status;
- output hash and consistency;
- median / p95 wall time;
- model/tool call counts;
- reported cost.

Deltas are relative to an explicit baseline. Output-hash equality is a deterministic comparison signal, not a semantic quality score.

## Lineage

`RunState v5` records:

- `parent_run_id`;
- `root_run_id`;
- `lineage_depth`;
- `fork_reason`;
- `replayed_from` where applicable.

Replay/fork always creates a new run and never mutates its source. Sub-playbooks also preserve parent/root lineage. This makes provenance inspectable across experimental variants and recovery operations.
