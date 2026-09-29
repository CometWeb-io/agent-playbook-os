"""Provider-neutral boundary between Playbook OS and an Agent Skills host.

This module deliberately contains no Claude, Codex, ChatGPT or vendor SDK code.
The host owns process selection, provider permissions, cancellation and any
host-specific approval UI. Playbook OS owns the compiled lock check and forwards
the control-plane identity needed for durable reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from types import MappingProxyType
from collections.abc import Mapping as MappingABC
from typing import Any, Mapping, Protocol

from agent_playbook_os.errors import ExternalExecutionRequired
from agent_playbook_os.models import RuntimeResult, SkillLockEntry, StepSpec
from agent_playbook_os.runtime import ReferenceRuntime
from agent_playbook_os.skills import SkillResolver
from agent_playbook_os.capabilities import CapabilityDescriptor, CapabilityRegistry


@dataclass(frozen=True)
class SkillInvocation:
    """Immutable identity and context handed to the external skill host."""

    skill_id: str
    skill_path: str
    content_hash: str
    invocation_id: str
    side_effects: str
    inputs: Mapping[str, Any]
    context: Mapping[str, Any]
    idempotency_key: str | None = None


class AgentSkillsHost(Protocol):
    """Host-owned execution hook; the host may return any runtime outcome."""

    async def invoke_skill(self, invocation: SkillInvocation) -> RuntimeResult | Any: ...


def _freeze(value: Any) -> Any:
    """Make nested invocation payloads read-only before crossing the host boundary."""
    if isinstance(value, MappingABC):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


class AgentSkillsRuntime(ReferenceRuntime):
    """Reference runtime that delegates locked skills to an external host.

    ``skill_locks`` must come from the compiled plan. The resolver is used at
    invocation time to detect package removal, replacement or content drift.
    Provider adapters should implement ``AgentSkillsHost`` and be injected by
    the application; this class never starts a provider process itself.
    """

    def __init__(
        self,
        *,
        host: AgentSkillsHost,
        resolver: SkillResolver,
        skill_locks: Mapping[str, SkillLockEntry],
        **runtime_options: Any,
    ):
        super().__init__(**runtime_options)
        self.host = host
        self.resolver = resolver
        self.skill_locks = dict(skill_locks)

    def capabilities(self) -> CapabilityRegistry:
        items = super().capabilities().list()
        if not self.dry_run:
            items.append(CapabilityDescriptor(
                id="skill-runtime:agent-skills", kind="skill-runtime",
                description="Locked Agent Skills delegated to an injected host.",
            ))
        return CapabilityRegistry(items)

    async def execute_skill(self, step: StepSpec, inputs: dict[str, Any], context: dict[str, Any]) -> Any:
        if self.dry_run:
            return await super().execute_skill(step, inputs, context)
        skill_id = step.uses
        if not skill_id:
            raise ExternalExecutionRequired("skill invocation is missing a skill id")
        lock = self.skill_locks.get(skill_id)
        if lock is None or not lock.resolved:
            raise ExternalExecutionRequired(f"skill lock is unresolved: {skill_id}")
        valid, reason = self.resolver.verify_lock(lock)
        if not valid:
            raise ExternalExecutionRequired(reason or f"skill lock verification failed: {skill_id}")
        if not lock.path or not lock.content_hash:
            raise ExternalExecutionRequired(f"skill lock is missing path or content hash: {skill_id}")

        control = context.get("control", {})
        invocation_id = control.get("invocation_id")
        if not invocation_id:
            raise ExternalExecutionRequired("skill invocation is missing a control-plane invocation id")
        invocation = SkillInvocation(
            skill_id=skill_id,
            skill_path=lock.path,
            content_hash=lock.content_hash,
            invocation_id=str(invocation_id),
            side_effects=step.side_effects,
            inputs=_freeze(inputs),
            context=_freeze(context),
            idempotency_key=control.get("idempotency_key"),
        )
        result = self.host.invoke_skill(invocation)
        if inspect.isawaitable(result):
            return await result
        return result
