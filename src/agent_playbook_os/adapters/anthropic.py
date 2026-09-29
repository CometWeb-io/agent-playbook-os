from __future__ import annotations

from typing import Any

from ..capabilities import CapabilityDescriptor, CapabilityRegistry
from ..models import ProviderReceipt, RuntimeResult, UsageMetrics
from ..runtime import CallableRuntime
from ._common import invoke_client, get_value, objective_text, jsonable_output


class AnthropicMessagesRuntime(CallableRuntime):
    """Thin adapter for an injected Anthropic-compatible ``client.messages`` object."""

    def __init__(self, client: Any, *, model: str, max_tokens: int = 2048, skills=None, actions=None, isolation_modes=None):
        super().__init__(skills=skills, actions=actions, isolation_modes=isolation_modes or {"inline", "auto"})
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def capabilities(self) -> CapabilityRegistry:
        items = super().capabilities().list()
        items += [
            CapabilityDescriptor(id="provider:anthropic", kind="provider", description="Injected Anthropic Messages client."),
            CapabilityDescriptor(id="agent-runtime:anthropic-messages", kind="agent-runtime", description="Anthropic Messages agent execution."),
            CapabilityDescriptor(id="receipt:provider-call-id", kind="receipt", description="Persists provider message IDs as receipts."),
        ]
        return CapabilityRegistry(items)

    async def execute_agent(self, step, inputs, context):
        create = self.client.messages.create
        response = await invoke_client(create,
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": objective_text(step, inputs)}],
        )
        content = get_value(response, "content", []) or []
        pieces = []
        for item in content:
            text = get_value(item, "text")
            if text is not None:
                pieces.append(str(text))
        output = "".join(pieces) if pieces else jsonable_output(response)
        usage_obj = get_value(response, "usage", {}) or {}
        usage = UsageMetrics(
            model_calls=1,
            input_tokens=int(get_value(usage_obj, "input_tokens", 0) or 0),
            output_tokens=int(get_value(usage_obj, "output_tokens", 0) or 0),
        )
        raw_call_id = get_value(response, "id")
        receipt = ProviderReceipt(
            provider="anthropic",
            call_id=str(raw_call_id),
            operation="messages.create",
            status="completed",
            metadata={"model": self.model},
        )
        receipts = [receipt] if raw_call_id is not None else []
        return RuntimeResult(value=output, usage=usage, provider_receipts=receipts)
