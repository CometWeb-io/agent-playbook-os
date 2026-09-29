from __future__ import annotations

from typing import Any

from ..capabilities import CapabilityDescriptor, CapabilityRegistry
from ..runtime import CallableRuntime


class CursorHostRuntime(CallableRuntime):
    """Host bridge for Cursor/IDE integrations supplied by the embedding process.

    Cursor does not expose a stable public Python agent SDK through this package, so
    this adapter intentionally models the host boundary instead of shelling out to an
    undocumented command. The host injects action/skill/agent handlers and explicitly
    declares which isolation boundaries it can actually provide.
    """

    def __init__(self, *, skills=None, actions=None, agent_handler=None, isolation_modes=None, extra_capabilities=None):
        super().__init__(
            skills=skills,
            actions=actions,
            agent_handler=agent_handler,
            isolation_modes=isolation_modes or {"inline", "auto"},
            extra_capabilities=extra_capabilities,
        )

    def capabilities(self) -> CapabilityRegistry:
        items = super().capabilities().list()
        items.append(CapabilityDescriptor(id="provider:cursor-host", kind="provider", description="Cursor/IDE host-injected runtime boundary."))
        return CapabilityRegistry(items)
