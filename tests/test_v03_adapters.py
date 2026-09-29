import asyncio
import json

from agent_playbook_os.benchmark import benchmark_plan
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.conformance import run_runtime_conformance
from agent_playbook_os.engine import Runner
from agent_playbook_os.integrity import run_state_integrity
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import CallableRuntime, ReferenceRuntime
from agent_playbook_os.store import RunStore


def make_pb():
    return Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1","kind":"Playbook",
        "metadata":{"id":"adapter","version":"0.3.0","description":"adapter"},
        "spec":{"steps":[{"id":"x","type":"action","action":"set","with":{"value":1}}]},
    })


def test_callable_runtime_bridges_host_handler(tmp_path):
    async def custom(step, inputs, context):
        return inputs["value"] * 2
    runtime = CallableRuntime(actions={"double": custom})
    playbook = Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1","kind":"Playbook",
        "metadata":{"id":"callable","version":"0.3.0","description":"callable"},
        "spec":{"steps":[{"id":"x","type":"action","action":"double","with":{"value":3}}],
                "outputs":{"value":"{{ steps.x.output }}"}},
    })
    state = asyncio.run(Runner(runtime).run(Compiler().compile(playbook), tmp_path / "run"))
    assert state.outputs["value"] == 6


def test_reference_runtime_conformance_passes():
    report = asyncio.run(run_runtime_conformance(ReferenceRuntime(dry_run=True)))
    assert report.passed is True


def test_benchmark_returns_repeatable_summary(tmp_path):
    plan = Compiler().compile(make_pb())
    report = asyncio.run(benchmark_plan(plan, lambda: ReferenceRuntime(), repeats=3, root=tmp_path / "bench"))
    assert report.repeats == 3
    assert report.succeeded == 3
    assert report.failed == 0
    assert report.median_ms >= 0


def test_v2_state_snapshot_migrates_to_current(tmp_path):
    plan = Compiler().compile(make_pb())
    run_dir = tmp_path / "run"
    asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir))
    store = RunStore(run_dir)
    raw = json.loads(store.state_path.read_text())
    for key in ["nested_steps", "invocations", "cancellation_reason", "cancelled_at"]:
        raw.pop(key, None)
    raw["schema_version"] = "agent-playbook-os/run-state/v2"
    raw["integrity_hash"] = "pending"
    raw["integrity_hash"] = run_state_integrity(raw)
    store.state_path.write_text(json.dumps(raw))
    assert store.verify_state_integrity() == (True, None)
    migrated = store.load_state()
    assert migrated.schema_version == "agent-playbook-os/run-state/v5"
    assert migrated.status == RunStatus.COMPLETED


def test_benchmark_can_preapprove_human_gate(tmp_path):
    playbook = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "bench-gate", "version": "0.3.0", "description": "benchmark gate"},
        "spec": {
            "steps": [
                {"id": "approval", "type": "gate", "gate": "human"},
                {"id": "done", "type": "action", "action": "set", "needs": ["approval"], "with": {"value": True}},
            ]
        },
    })
    plan = Compiler().compile(playbook)
    report = asyncio.run(benchmark_plan(
        plan,
        lambda: ReferenceRuntime(),
        repeats=2,
        root=tmp_path / "bench-gate",
        approvals={"approval"},
        approval_actor="benchmark",
    ))
    assert report.succeeded == 2
    assert report.failed == 0
