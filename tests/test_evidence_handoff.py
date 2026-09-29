"""Execute the shipped example against the Evidence Graph v2 handoff shape."""
from pathlib import Path

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.loader import load_playbook
from agent_playbook_os.models import RunStatus, StepStatus
from agent_playbook_os.runtime import MockRuntime


EXAMPLE = Path(__file__).resolve().parents[1] / "playbooks/examples/evidence-to-decision.yaml"


def pack(status):
    # Synthetic shape, not a claim that empty research is sufficient for READY.
    return {"schema_version": "2.0", "research_id": "fixture", "research_contract": {},
            "claims": [], "sources": [], "evidence": [], "contradictions": [],
            "searches": [], "gaps": [], "research_status": status, "stop_reason": None}


async def execute(tmp_path, output):
    plan = Compiler().compile(load_playbook(EXAMPLE), {"question": "fixture"}, source_path=str(EXAMPLE))
    runtime = MockRuntime(skill_outputs={"research": output, "decision": {"decision": "fixture"}}, isolation_modes={"inline", "auto", "subagent"})
    state = await Runner(runtime).run(plan, tmp_path / "run")
    return state, runtime


@pytest.mark.asyncio
async def test_ready_pack_reaches_decision_with_original_evidence(tmp_path):
    output = pack("READY")
    state, runtime = await execute(tmp_path, output)
    assert state.status == RunStatus.COMPLETED
    assert state.outputs["decision"] == {"decision": "fixture"}
    assert runtime.calls[-1][2]["evidence"] == output


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PARTIAL", "REFRESH_REQUIRED", "BLOCKED_BY_CONTRADICTION"])
async def test_nonready_pack_stops_at_gate_without_decision(tmp_path, status):
    state, runtime = await execute(tmp_path, pack(status))
    assert state.steps["research"].status == StepStatus.COMPLETED
    assert state.steps["evidence-ready"].status == StepStatus.FAILED
    assert "AttributeError" not in state.steps["evidence-ready"].error
    assert [call[1] for call in runtime.calls] == ["research"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_status", "unknown_status", "unknown_version", "invalid_claims"])
async def test_invalid_evidence_shape_fails_at_producer_boundary(tmp_path, mutation):
    output = pack("READY")
    if mutation == "missing_status":
        del output["research_status"]
    elif mutation == "unknown_status":
        output["research_status"] = "LOOKS_GOOD"
    elif mutation == "unknown_version":
        output["schema_version"] = "999"
    else:
        output["claims"] = "not-an-array"
    state, runtime = await execute(tmp_path, output)
    assert state.steps["research"].status == StepStatus.FAILED
    assert "ValidationError" in state.steps["research"].error
    assert [call[1] for call in runtime.calls] == ["research"]
