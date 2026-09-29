from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.distributed import (
    DistributedWorker, SQLiteFencingCoordinator, SQLiteWorkQueue, WorkSubmission,
    required_capabilities_for_plan,
)
from agent_playbook_os.engine import Runner
from agent_playbook_os.loader import load_playbook
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore


async def main():
    root = Path(tempfile.mkdtemp(prefix="apbos-distributed-smoke-"))
    pb = load_playbook(Path(__file__).resolve().parents[1] / "playbooks/examples/kernel-demo.yaml")
    plan = Compiler().compile(pb, {"name": "Ada"})
    plan_path = root / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    run_dir = root / "runs" / "acme" / "prod" / "run-1"
    q = SQLiteWorkQueue(root / "queue.sqlite3")
    q.enqueue(WorkSubmission(
        work_id="smoke-work", tenant_id="acme", namespace="prod", run_id="run-1",
        plan_path=str(plan_path), run_dir=str(run_dir),
        required_capabilities=required_capabilities_for_plan(plan),
        approvals=["approval"],
    ))
    runtime = ReferenceRuntime()
    def runner_factory(store_backend="filesystem", item=None):
        return Runner(runtime, store_factory=RunStore, enforce_capabilities=True)
    worker = DistributedWorker(
        queue=q,
        runtime=runtime,
        runner_factory=runner_factory,
        worker_id="smoke-worker",
        visibility_timeout_seconds=2.0,
        coordinator=SQLiteFencingCoordinator(root / "fences.sqlite3"),
        allowed_run_root=root / "runs",
        allowed_plan_root=root,
    )
    results = await worker.run_once()
    if len(results) != 1 or results[0].error:
        raise SystemExit(f"distributed worker failed: {results}")
    state = RunStore(run_dir).load_state()
    if state.status.value != "COMPLETED":
        raise SystemExit(f"unexpected state: {state.status.value}")
    if q.get("smoke-work").status != "COMPLETED":
        raise SystemExit("queue work was not acknowledged")
    print("DISTRIBUTED_SMOKE_OK")


if __name__ == "__main__":
    asyncio.run(main())
