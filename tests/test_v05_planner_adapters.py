import asyncio
import json

from agent_playbook_os.adapters.planner import OpenAIPlannerRuntime, AnthropicPlannerRuntime
from agent_playbook_os.planning import PlannerRequest


class Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def response_payload():
    return {
        "mode": "none",
        "confidence": "none",
        "rationale": "no suitable playbook",
        "metadata": {"test": True},
    }


def test_openai_planner_uses_injected_client_without_sdk_import():
    calls = []
    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return Obj(output_text=json.dumps(response_payload()))
    client = Obj(responses=Responses())
    planner = OpenAIPlannerRuntime(client, model="test-model")
    out = asyncio.run(planner.plan(PlannerRequest(goal="x")))
    assert out.mode == "none"
    assert calls[0]["model"] == "test-model"
    assert "Return JSON only" in calls[0]["input"]


def test_anthropic_planner_uses_injected_client_without_sdk_import():
    calls = []
    class Messages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return Obj(content=[Obj(text=json.dumps(response_payload()))])
    client = Obj(messages=Messages())
    planner = AnthropicPlannerRuntime(client, model="test-model")
    out = asyncio.run(planner.plan(PlannerRequest(goal="x")))
    assert out.mode == "none"
    assert calls[0]["model"] == "test-model"
    assert calls[0]["max_tokens"] == 4096
