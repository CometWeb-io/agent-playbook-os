import asyncio
import json

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.diffing import diff_runs
from agent_playbook_os.engine import Runner
from agent_playbook_os.evals import run_eval_suite
from agent_playbook_os.models import Playbook
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore


def make_pb(value):
    return Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": "diff", "version": "0.2.0", "description": "diff"},
        "spec": {
            "steps": [{"id": "a", "type": "action", "action": "set", "with": {"value": value}}],
            "outputs": {"value": "{{ steps.a.output }}"},
        },
    })


def test_state_integrity_detects_tamper(tmp_path):
    plan = Compiler().compile(make_pb(1))
    run_dir = tmp_path / "run"
    asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir))
    store = RunStore(run_dir)
    assert store.verify_state_integrity() == (True, None)
    raw = json.loads(store.state_path.read_text())
    raw["outputs"]["value"] = 999
    store.state_path.write_text(json.dumps(raw))
    ok, error = store.verify_state_integrity()
    assert ok is False
    assert "integrity_hash mismatch" in error


def test_run_diff_detects_output_change(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    asyncio.run(Runner(ReferenceRuntime()).run(Compiler().compile(make_pb(1)), a))
    asyncio.run(Runner(ReferenceRuntime()).run(Compiler().compile(make_pb(2)), b))
    diff = diff_runs(a, b)
    assert diff.outputs_changed is True
    assert diff.step_diffs[0].step_id == "a"
    assert diff.step_diffs[0].output_changed is True


def test_kernel_eval_suite_passes():
    result = asyncio.run(run_eval_suite("evals/cases/kernel.jsonl"))
    assert result.failed == 0
    assert result.total == 5


def test_adversarial_eval_suite_passes():
    result = asyncio.run(run_eval_suite("evals/cases/adversarial.jsonl"))
    assert result.failed == 0
    assert result.total == 5
