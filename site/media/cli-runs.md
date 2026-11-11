# How the landing page values were produced

Every value on the landing page (`site/index.html`) comes from the runs below.
They were made on 11 Nov 2026 with Agent Playbook OS 0.6.0 at commit `65a6185`,
installed from a clone, on macOS with Python 3.14. All runs use the built-in
reference runtime, which calls no model. Run IDs, timestamps and hashes will
differ on your machine; statuses, outputs and counts should not.

## 1. Run record: `kernel-demo`

```bash
playbook run playbooks/examples/kernel-demo.yaml --input name=Ada --run-root runs
# "status": "WAITING_APPROVAL"  (event 11 is gate.approval_required)
playbook resume runs/<RUN> --approve approval --approval-actor reviewer@example.com
# "status": "COMPLETED", "outputs": {"result": "approved for Ada"}
playbook verify runs/<RUN>
# "ok": true — event_chain, plan_integrity, state_integrity, cross_refs, artifacts
playbook inspect runs/<RUN>
# "event_count": 28, "step_count": 5, "invocation_count": 3
```

The approval was given about 10 seconds after the first run stopped
(`gate.approved` at 14:17:42 UTC). The trace after `run.resumed` starts only the
`approval`, `final` and `verify` steps; `greeting` and `choose` are not run
again. The hero animation replays this run with the wait shortened. Its event
counts are the event sequence numbers at the end of each stage: 7 (`greeting`
done), 10 (`choose` done), 11 (approval required), 16 (`approval` done),
22 (`final` done) and 28 (`run.completed`).

## 2. Plan for review: `evidence-to-decision`

```bash
git clone --depth 1 https://github.com/CometWeb-io/agent-skills   # ea92513
playbook compile playbooks/examples/evidence-to-decision.yaml \
  --input question='"Should we ship?"' \
  --skills-root agent-skills --strict-skills --out plan.json
playbook skills verify-plan plan.json agent-skills
# "ok": true for ai-council and evidence-researcher
```

`plan.json` pins `evidence-researcher` 1.0.11 and `ai-council` 5.3.2, has a
condition gate on `research_status == 'READY'`, and runs the `decision` step
with `execution_isolation: subagent`. Nothing was executed.

## 3. Interrupted run: `send-invoice.yaml`

[`send-invoice.yaml`](send-invoice.yaml) is a test playbook written for this
page. Its `send` step uses the built-in `sleep` action, declared as an
`external` side effect, to stand in for a real outside call.

```bash
playbook run site/media/send-invoice.yaml --input customer=Acme --run-root runs \
  --approve send --approval-actor ops@example.com &
sleep 2.5; kill -9 %1          # the process dies while `send` is running
playbook resume runs/<RUN>
# ReconciliationRequired: uncertain external invocation <ID> for send;
# reconcile before resume
playbook reconcile runs/<RUN> <ID> --outcome not-executed --actor ops@example.com
playbook resume runs/<RUN> --approve send --approval-actor ops@example.com
# "status": "COMPLETED"; playbook verify → "ok": true
```

Without `--approve send`, the same playbook stops at `WAITING_APPROVAL` before
`send`, because it is declared as an external side effect.

## 4. Edited record

A copy of the run from section 1, with `reviewer@example.com` replaced in
`events.jsonl`:

```bash
cp -R runs/<RUN> tampered
sed -i '' 's/reviewer@example.com/someone-else@example.com/' tampered/events.jsonl
playbook verify tampered
# "ok": false, event_chain: "event_hash mismatch at seq=13"; the other four
# checks pass; exit code 1
```

## Other numbers on the page

- **10 kinds of step**: the `type` literal on steps in
  `src/agent_playbook_os/models.py`.
- **3 runtime dependencies**: `[project] dependencies` in `pyproject.toml`
  (Pydantic, PyYAML, jsonschema).
- **Install commands**: run on a fresh clone of `main` at `65a6185` with
  `uv sync` and `uv run playbook …`.
