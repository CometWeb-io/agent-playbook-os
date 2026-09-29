import asyncio, json
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.store import RunStore


def test_event_chain_detects_tampering(tmp_path):
    pb = Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1","kind":"Playbook",
        "metadata":{"id":"chain","version":"0.1.0","description":"chain"},
        "spec":{"steps":[{"id":"x","type":"action","action":"set","with":{"value":1}}]}
    })
    plan = Compiler().compile(pb)
    run_dir = tmp_path / "r"
    asyncio.run(Runner(ReferenceRuntime()).run(plan, run_dir))
    store = RunStore(run_dir)
    assert store.verify_event_chain() == (True, None)
    lines = store.events_path.read_text().splitlines()
    item = json.loads(lines[1])
    item["type"] = "tampered"
    lines[1] = json.dumps(item)
    store.events_path.write_text("\n".join(lines)+"\n")
    ok, error = store.verify_event_chain()
    assert ok is False
    assert "event_hash mismatch" in error
