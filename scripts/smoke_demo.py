from pathlib import Path
import asyncio, tempfile
from agent_playbook_os.loader import load_playbook
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.runtime import ReferenceRuntime

root = Path(__file__).resolve().parents[1]
p = root / "playbooks/examples/kernel-demo.yaml"
pb = load_playbook(p)
plan = Compiler().compile(pb, {"name":"Smoke"}, source_path=str(p))
with tempfile.TemporaryDirectory() as d:
    first = asyncio.run(Runner(ReferenceRuntime()).run(plan, Path(d)/"run"))
    assert first.status.value == "WAITING_APPROVAL"
    second = asyncio.run(Runner(ReferenceRuntime()).run(plan, Path(d)/"run", approvals={"approval"}, resume=True))
    assert second.status.value == "COMPLETED"
    assert second.outputs["result"] == "approved for Smoke"
print("SMOKE_OK")
