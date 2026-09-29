from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from typing import Any, Callable


PLUGIN_GROUPS = {
    "runtime": "agent_playbook_os.runtimes",
    "store": "agent_playbook_os.stores",
    "telemetry": "agent_playbook_os.telemetry",
    "secrets": "agent_playbook_os.secrets",
    "schema_resolver": "agent_playbook_os.schema_resolvers",
    "planner": "agent_playbook_os.planners",
    "queue": "agent_playbook_os.queues",
    "fencing": "agent_playbook_os.fencing",
    "artifact_store": "agent_playbook_os.artifact_stores",
    "tenant_policy": "agent_playbook_os.tenant_policies",
}


@dataclass(frozen=True)
class PluginDescriptor:
    kind: str
    name: str
    value: str
    distribution: str | None = None


class PluginRegistry:
    """Discover optional host integrations through Python entry points.

    Core never imports provider SDKs during discovery. A plugin is loaded only when a
    caller explicitly asks for it. This keeps provider dependencies outside the kernel.
    """

    def __init__(self):
        self._manual: dict[tuple[str, str], Callable[..., Any] | Any] = {}

    def register(self, kind: str, name: str, factory: Callable[..., Any] | Any) -> None:
        if kind not in PLUGIN_GROUPS:
            raise ValueError(f"unknown plugin kind: {kind}")
        if not name:
            raise ValueError("plugin name must not be empty")
        key = (kind, name)
        if key in self._manual:
            raise ValueError(f"duplicate manual plugin: {kind}:{name}")
        self._manual[key] = factory

    def descriptors(self, kind: str | None = None) -> list[PluginDescriptor]:
        kinds = [kind] if kind else sorted(PLUGIN_GROUPS)
        out: list[PluginDescriptor] = []
        eps = metadata.entry_points()
        for current in kinds:
            if current not in PLUGIN_GROUPS:
                raise ValueError(f"unknown plugin kind: {current}")
            group = PLUGIN_GROUPS[current]
            selected = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])
            for ep in selected:
                dist_name = getattr(getattr(ep, "dist", None), "name", None)
                out.append(PluginDescriptor(current, ep.name, ep.value, dist_name))
            for (manual_kind, name), value in self._manual.items():
                if manual_kind == current:
                    out.append(PluginDescriptor(current, name, f"manual:{type(value).__name__}", None))
        return sorted(out, key=lambda x: (x.kind, x.name, x.value))

    def load(self, kind: str, name: str) -> Any:
        if kind not in PLUGIN_GROUPS:
            raise ValueError(f"unknown plugin kind: {kind}")
        manual = self._manual.get((kind, name))
        eps = metadata.entry_points()
        selected = eps.select(group=PLUGIN_GROUPS[kind], name=name) if hasattr(eps, "select") else [
            ep for ep in eps.get(PLUGIN_GROUPS[kind], []) if ep.name == name
        ]
        matches = list(selected)
        if manual is not None and matches:
            raise RuntimeError(f"ambiguous plugin: {kind}:{name} exists as both manual and entry-point registration")
        if manual is not None:
            return manual
        if not matches:
            raise KeyError(f"plugin not found: {kind}:{name}")
        if len(matches) != 1:
            raise RuntimeError(f"ambiguous plugin: {kind}:{name}")
        return matches[0].load()

    def create(self, kind: str, name: str, **kwargs: Any) -> Any:
        factory = self.load(kind, name)
        return factory(**kwargs) if callable(factory) else factory
