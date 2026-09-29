from __future__ import annotations

from typing import Any

from ..capabilities import CapabilityDescriptor, CapabilityRegistry
from ..models import ProviderReceipt, RuntimeResult, UsageMetrics
from ..runtime import CallableRuntime
from ._common import invoke_client, get_value, objective_text, jsonable_output


class OpenAIResponsesRuntime(CallableRuntime):
    """Thin adapter for an injected OpenAI-compatible ``client.responses`` object.

    The OpenAI package is deliberately not imported by core. Sync and async clients are
    both supported structurally, which also makes the adapter easy to conformance-test
    with a fake client.
    """

    def __init__(self, client: Any, *, model: str, skills=None, actions=None, isolation_modes=None):
        super().__init__(skills=skills, actions=actions, isolation_modes=isolation_modes or {"inline", "auto"})
        self.client = client
        self.model = model

    def capabilities(self) -> CapabilityRegistry:
        items = super().capabilities().list()
        items += [
            CapabilityDescriptor(id="provider:openai", kind="provider", description="Injected OpenAI Responses client."),
            CapabilityDescriptor(id="agent-runtime:openai-responses", kind="agent-runtime", description="OpenAI Responses agent execution."),
            CapabilityDescriptor(id="receipt:provider-call-id", kind="receipt", description="Persists provider response IDs as receipts."),
        ]
        return CapabilityRegistry(items)

    async def execute_agent(self, step, inputs, context):
        create = self.client.responses.create
        response = await invoke_client(create, model=self.model, input=objective_text(step, inputs))
        output = get_value(response, "output_text")
        if output is None:
            output = jsonable_output(get_value(response, "output", response))
        usage_obj = get_value(response, "usage", {}) or {}
        usage = UsageMetrics(
            model_calls=1,
            input_tokens=int(get_value(usage_obj, "input_tokens", 0) or 0),
            output_tokens=int(get_value(usage_obj, "output_tokens", 0) or 0),
        )
        raw_call_id = get_value(response, "id")
        receipt = ProviderReceipt(
            provider="openai",
            call_id=str(raw_call_id),
            operation="responses.create",
            status="completed",
            metadata={"model": self.model},
        )
        receipts = [receipt] if raw_call_id is not None else []
        return RuntimeResult(value=output, usage=usage, provider_receipts=receipts)
