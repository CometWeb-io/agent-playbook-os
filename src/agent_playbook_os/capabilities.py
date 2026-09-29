from __future__ import annotations

from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field


class CapabilityDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["action", "skill-runtime", "agent-runtime", "eval-runtime", "approval", "storage", "trace", "isolation", "streaming", "receipt", "secrets", "cancellation", "provider"]
    description: str
    side_effects: Literal["none", "local", "external", "destructive"] = "none"
    supports_idempotency: bool = False
    supports_dry_run: bool = False
    metadata: dict = Field(default_factory=dict)


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[CapabilityDescriptor] = ()):
        self._items = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: CapabilityDescriptor):
        if capability.id in self._items:
            raise ValueError(f"duplicate capability id: {capability.id}")
        self._items[capability.id] = capability

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        return self._items.get(capability_id)

    def list(self) -> list[CapabilityDescriptor]:
        return [self._items[k] for k in sorted(self._items)]
