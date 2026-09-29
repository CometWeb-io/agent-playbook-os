import pytest
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime, MockRuntime


def make(steps, deps=None):
    return Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1","kind":"Playbook",
        "metadata":{"id":"flow","version":"0.1.0","description":"flow test"},
        "spec":{"dependencies":{"skills":deps or {}},"steps":steps}
    })


@pytest.mark.asyncio
async def test_branch(tmp_path):
    p = make([
      {"id":"a","type":"action","action":"set","with":{"value":{"n":2}}},
      {"id":"b","type":"branch","needs":["a"],"cases":[{"when":"steps.a.output.n == 2","value":"two"}],"default":"other"},
    ])
    state = await Runner(ReferenceRuntime()).run(Compiler().compile(p), tmp_path / "r")
    assert state.steps["b"].output["selected"] == "two"


@pytest.mark.asyncio
async def test_parallel(tmp_path):
    p = make([
      {"id":"p","type":"parallel","branches":{
        "one":[{"id":"x","type":"action","action":"set","with":{"value":1}}],
        "two":[{"id":"y","type":"action","action":"set","with":{"value":2}}],
      }}
    ])
    state = await Runner(ReferenceRuntime()).run(Compiler().compile(p), tmp_path / "r")
    assert state.status == RunStatus.COMPLETED
    assert state.steps["p"].output == {"one":{"x":1},"two":{"y":2}}
