from __future__ import annotations

import json
from typing import Any

from ..planning import PlannerRequest, PlannerResponse, planner_prompt
from ._common import invoke_client, get_value


class OpenAIPlannerRuntime:
    """Injected-client planner for OpenAI Responses-compatible clients."""

    def __init__(self, client: Any, *, model: str):
        self.client = client
        self.model = model

    async def plan(self, request: PlannerRequest) -> PlannerResponse:
        response = await invoke_client(
            self.client.responses.create, model=self.model, input=planner_prompt(request)
        )
        text = get_value(response, "output_text")
        if not isinstance(text, str):
            raise ValueError("OpenAI planner response did not provide output_text")
        return PlannerResponse.model_validate(json.loads(text))


class AnthropicPlannerRuntime:
    """Injected-client planner for Anthropic Messages-compatible clients."""

    def __init__(self, client: Any, *, model: str, max_tokens: int = 4096):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    async def plan(self, request: PlannerRequest) -> PlannerResponse:
        response = await invoke_client(
            self.client.messages.create,
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": planner_prompt(request)}],
        )
        content = get_value(response, "content", []) or []
        pieces = []
        for item in content:
            text = get_value(item, "text")
            if text is not None:
                pieces.append(str(text))
        if not pieces:
            raise ValueError("Anthropic planner response did not contain text content")
        return PlannerResponse.model_validate(json.loads("".join(pieces)))
