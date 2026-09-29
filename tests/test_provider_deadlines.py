"""The injected SDK boundary must not block the runner's control plane."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from agent_playbook_os.adapters.openai import OpenAIResponsesRuntime
from agent_playbook_os.adapters.anthropic import AnthropicMessagesRuntime
from agent_playbook_os.adapters.planner import OpenAIPlannerRuntime, AnthropicPlannerRuntime
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.models import Playbook, StepSpec, InvocationStatus
from agent_playbook_os.planning import PlannerRequest


def response(provider, planner=False):
    text = json.dumps({"mode": "none", "confidence": "none", "rationale": "fixture"}) if planner else "fixture"
    return {"id": "fixture-id", "output_text": text, "content": [{"text": text}], "usage": {"input_tokens": 4, "output_tokens": 2}}


def adapter(provider, create, planner=False):
    client = SimpleNamespace(**{"responses" if provider == "openai" else "messages": SimpleNamespace(create=create)})
    cls = ({"openai": OpenAIPlannerRuntime, "anthropic": AnthropicPlannerRuntime} if planner else {"openai": OpenAIResponsesRuntime, "anthropic": AnthropicMessagesRuntime})[provider]
    return cls(client, model="fixture-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("planner", [False, True])
async def test_sync_sdk_does_not_swallow_caller_deadline(provider, planner):
    def create(**kwargs):
        time.sleep(0.15)
        return response(provider, planner)
    runtime = adapter(provider, create, planner)
    call = runtime.plan(PlannerRequest(goal="fixture")) if planner else runtime.execute_agent(StepSpec(id="x", type="agent"), {}, {})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call, 0.02)
    # A thread deadline is not remote cancellation. Let this bounded fixture exit.
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_async_sdk_preserves_value_usage_and_receipt(provider):
    async def create(**kwargs):
        await asyncio.sleep(0)
        return response(provider)
    output = await adapter(provider, create).execute_agent(StepSpec(id="x", type="agent"), {}, {})
    assert output.value == "fixture"
    assert output.usage.input_tokens == 4
    assert output.usage.output_tokens == 2
    assert output.provider_receipts[0].call_id == "fixture-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_sync_timeout_keeps_material_outcome_unknown_without_retry(tmp_path, provider):
    def create(**kwargs):
        time.sleep(0.15)
        return response(provider)
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "sdk-deadline", "version": "0.1.0", "description": "deadline regression"},
        "spec": {"policy": {"require_approval_for": []}, "steps": [{
            "id": "external", "type": "agent", "side_effects": "external",
            "timeout_seconds": 0.02, "idempotency_key": "stable", "retry": {"max_attempts": 2},
        }]},
    })
    state = await Runner(adapter(provider, create)).run(Compiler().compile(pb), tmp_path / "run")
    await asyncio.sleep(0.2)
    assert state.invocations[0].status == InvocationStatus.UNKNOWN
    assert state.steps["external"].attempts == 1
    assert not state.provider_receipts, "a late provider response must not certify timed-out work"
