from __future__ import annotations

import asyncio
import math
from typing import Any, Protocol

from .capabilities import CapabilityDescriptor, CapabilityRegistry
from .commands import CommandAllowlist, CommandOutputLimitExceeded, run_command
from .errors import ExternalExecutionRequired
from .models import RuntimeResult, StepSpec


class ExecutionRuntime(Protocol):
    async def execute_skill(self, step: StepSpec, inputs: dict[str, Any], context: dict[str, Any]) -> Any: ...
    async def execute_action(self, step: StepSpec, inputs: dict[str, Any], context: dict[str, Any]) -> Any: ...
    async def execute_agent(self, step: StepSpec, inputs: dict[str, Any], context: dict[str, Any]) -> Any: ...
    async def execute_eval(self, step: StepSpec, inputs: dict[str, Any], context: dict[str, Any]) -> Any: ...


class ReferenceRuntime:
    """Safe provider-neutral reference runtime.

    Skill and agent execution are host boundaries. In dry-run mode skill, agent
    and command invocations are returned as structured plans. Command actions are disabled unless explicitly
    enabled, accept argv arrays only, and require a pinned executable allowlist.
    No shell is implicitly spawned.
    """

    def __init__(
        self,
        dry_run: bool = False,
        allow_commands: bool = False,
        command_cwd: str | None = None,
        allowed_commands: set[str] | None = None,
        max_command_output_bytes: int = 1_000_000,
    ):
        if allow_commands and not allowed_commands:
            raise ExternalExecutionRequired(
                "command actions require a non-empty executable allowlist"
            )
        if type(max_command_output_bytes) is not int or max_command_output_bytes <= 0:
            raise ValueError("max_command_output_bytes must be a positive integer")
        self.dry_run = dry_run
        self.allow_commands = allow_commands
        self.command_cwd = command_cwd
        self.allowed_commands = set(allowed_commands or set())
        self.max_command_output_bytes = max_command_output_bytes
        self._commands = CommandAllowlist(self.allowed_commands, command_cwd)

    def capabilities(self) -> CapabilityRegistry:
        items = [
            CapabilityDescriptor(id="action:set", kind="action", description="Return a supplied value."),
            CapabilityDescriptor(id="action:echo", kind="action", description="Echo structured inputs."),
            CapabilityDescriptor(id="action:sleep", kind="action", description="Deterministic async delay."),
            CapabilityDescriptor(
                id="eval-runtime:reference",
                kind="eval-runtime",
                description="Reference equality evaluator.",
            ),
            CapabilityDescriptor(
                id="isolation:inline",
                kind="isolation",
                description="Execute in the current host process/context.",
            ),
        ]
        if self.allow_commands:
            items.append(CapabilityDescriptor(
                id="action:command",
                kind="action",
                description="Opt-in argv-only local process execution without a shell.",
                side_effects="local",
                supports_idempotency=False,
                supports_dry_run=True,
            ))
        if self.dry_run:
            items.extend([
                CapabilityDescriptor(
                    id="skill-runtime:dry-run",
                    kind="skill-runtime",
                    description="Record typed skill invocation instead of executing it.",
                    supports_dry_run=True,
                ),
                CapabilityDescriptor(
                    id="agent-runtime:dry-run",
                    kind="agent-runtime",
                    description="Record typed agent invocation instead of executing it.",
                    supports_dry_run=True,
                ),
                CapabilityDescriptor(
                    id="isolation:sandbox",
                    kind="isolation",
                    description="Dry-run declaration only; no side effects are executed.",
                    supports_dry_run=True,
                ),
                CapabilityDescriptor(
                    id="isolation:subagent",
                    kind="isolation",
                    description="Dry-run declaration only; no side effects are executed.",
                    supports_dry_run=True,
                ),
            ])
        return CapabilityRegistry(items)



    def supports_isolation(self, mode: str, step=None) -> bool:
        if self.dry_run:
            return mode in {"inline", "auto", "sandbox", "subagent"}
        return mode in {"inline", "auto"}

    def supports_idempotency(self, step: StepSpec) -> bool:
        # Built-in reference actions with material side effects do not exist. Unknown
        # host actions must prove idempotency in their adapter rather than relying on
        # the presence of an idempotency key in the playbook.
        return False

    async def execute_skill(self, step, inputs, context):
        payload = {
            "kind": "skill-invocation",
            "skill": step.uses,
            "inputs": inputs,
            "isolation": step.execution_isolation or "auto",
            "idempotency_key": context.get("control", {}).get("idempotency_key"),
        }
        if self.dry_run:
            return payload
        raise ExternalExecutionRequired(
            f"skill '{step.uses}' requires a host adapter; rerun with --dry-run or inject ExecutionRuntime"
        )

    async def execute_agent(self, step, inputs, context):
        payload = {
            "kind": "agent-invocation",
            "objective": inputs.get("objective") or step.description,
            "inputs": inputs,
            "isolation": step.execution_isolation or "auto",
            "idempotency_key": context.get("control", {}).get("idempotency_key"),
        }
        if self.dry_run:
            return payload
        raise ExternalExecutionRequired("agent step requires a host adapter")

    async def execute_action(self, step, inputs, context):
        if step.action == "set":
            return inputs.get("value", inputs)
        if step.action == "echo":
            return {"echo": inputs}
        if step.action == "sleep":
            seconds = float(inputs.get("seconds", 0))
            if seconds < 0 or seconds > 3600:
                raise ValueError("sleep.seconds must be between 0 and 3600")
            await asyncio.sleep(seconds)
            return {"slept_seconds": seconds}
        if step.action == "command":
            if not self.allow_commands:
                raise ExternalExecutionRequired("command actions are disabled; pass allow_commands=True explicitly")
            argv = inputs.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                raise ValueError("command action requires with.argv as a non-empty string array")
            executable = self._commands.resolve(argv[0])
            timeout = float(inputs.get("timeout", 30))
            if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
                raise ValueError("command timeout must be in (0, 3600]")
            if self.dry_run:
                return {
                    "kind": "command-invocation", "dry_run": True, "argv": argv,
                    "executable": executable, "cwd": str(self._commands.cwd),
                    "timeout": timeout,
                }
            try:
                return await run_command(
                    argv, executable=executable, cwd=str(self._commands.cwd),
                    timeout=timeout, max_output=self.max_command_output_bytes,
                )
            except CommandOutputLimitExceeded as exc:
                if step.side_effects in {"local", "external", "destructive"}:
                    from .errors import SideEffectOutcomeUnknown
                    raise SideEffectOutcomeUnknown(str(exc)) from exc
                raise
        raise ExternalExecutionRequired(f"unknown action requires adapter: {step.action}")

    async def execute_eval(self, step, inputs, context):
        expected = inputs.get("expected")
        actual = inputs.get("actual")
        passed = actual == expected
        return {"passed": passed, "actual": actual, "expected": expected}


class MockRuntime(ReferenceRuntime):
    def __init__(self, skill_outputs=None, action_outputs=None, agent_outputs=None, *, isolation_modes=None, idempotent_steps=None):
        super().__init__(dry_run=True)
        self.isolation_modes = set(isolation_modes or {"inline", "auto"})
        self.idempotent_steps = set(idempotent_steps or set())
        self.skill_outputs = skill_outputs or {}
        self.action_outputs = action_outputs or {}
        self.agent_outputs = agent_outputs or {}
        self.calls = []

    def supports_isolation(self, mode: str, step=None) -> bool:
        return mode in self.isolation_modes or (mode == "auto" and "inline" in self.isolation_modes)

    def supports_idempotency(self, step: StepSpec) -> bool:
        return step.id in self.idempotent_steps

    def _resolve_output(self, source, key, fallback, step, inputs, context):
        value = source.get(key, fallback)
        if callable(value):
            return value(step, inputs, context)
        if isinstance(value, list):
            if not value:
                return fallback
            return value.pop(0)
        return value

    async def execute_skill(self, step, inputs, context):
        self.calls.append(("skill", step.id, inputs, context.get("control")))
        out = self._resolve_output(
            self.skill_outputs,
            step.id,
            self.skill_outputs.get(step.uses, {"ok": True}),
            step,
            inputs,
            context,
        )
        if isinstance(out, Exception):
            raise out
        return out

    async def execute_action(self, step, inputs, context):
        self.calls.append(("action", step.id, inputs, context.get("control")))
        if step.id in self.action_outputs:
            out = self._resolve_output(self.action_outputs, step.id, None, step, inputs, context)
            if isinstance(out, Exception):
                raise out
            return out
        return await super().execute_action(step, inputs, context)

    async def execute_agent(self, step, inputs, context):
        self.calls.append(("agent", step.id, inputs, context.get("control")))
        out = self._resolve_output(self.agent_outputs, step.id, {"ok": True}, step, inputs, context)
        if isinstance(out, Exception):
            raise out
        return out


class CallableRuntime(ReferenceRuntime):
    """Small host bridge built from injected callables.

    This is intentionally not an SDK-specific adapter. Hosts can map their own model,
    MCP, tool or subagent APIs into the stable ExecutionRuntime contract without
    importing provider dependencies into the core package.
    """

    def __init__(
        self,
        *,
        skills: dict[str, Any] | None = None,
        actions: dict[str, Any] | None = None,
        agent_handler=None,
        eval_handler=None,
        isolation_modes: set[str] | None = None,
        idempotent_actions: set[str] | None = None,
        extra_capabilities: list[CapabilityDescriptor] | None = None,
    ):
        super().__init__(dry_run=False)
        self.skill_handlers = dict(skills or {})
        self.action_handlers = dict(actions or {})
        self.agent_handler = agent_handler
        self.eval_handler = eval_handler
        self.isolation_modes = set(isolation_modes or {"inline", "auto"})
        self.idempotent_actions = set(idempotent_actions or set())
        self.extra_capabilities = list(extra_capabilities or [])

    async def _invoke(self, handler, step, inputs, context):
        if handler is None:
            raise ExternalExecutionRequired(f"no handler registered for step {step.id}")
        value = handler(step, inputs, context)
        if hasattr(value, "__await__"):
            value = await value
        return value

    def supports_isolation(self, mode: str, step=None) -> bool:
        return mode in self.isolation_modes or (mode == "auto" and "inline" in self.isolation_modes)

    def supports_idempotency(self, step: StepSpec) -> bool:
        return step.type == "action" and step.action in self.idempotent_actions

    def capabilities(self) -> CapabilityRegistry:
        items = []
        for action in sorted(self.action_handlers):
            items.append(CapabilityDescriptor(
                id=f"action:{action}",
                kind="action",
                description=f"Injected action handler: {action}",
                supports_idempotency=action in self.idempotent_actions,
            ))
        if self.skill_handlers:
            items.append(CapabilityDescriptor(
                id="skill-runtime:callable",
                kind="skill-runtime",
                description="Injected skill handlers.",
            ))
        if self.agent_handler:
            items.append(CapabilityDescriptor(
                id="agent-runtime:callable",
                kind="agent-runtime",
                description="Injected agent handler.",
            ))
        items.append(CapabilityDescriptor(
            id="eval-runtime:callable",
            kind="eval-runtime",
            description="Injected or reference eval handler.",
        ))
        for mode in sorted(self.isolation_modes - {"auto"}):
            items.append(CapabilityDescriptor(
                id=f"isolation:{mode}",
                kind="isolation",
                description=f"Host-provided {mode} execution boundary.",
            ))
        items.extend(self.extra_capabilities)
        return CapabilityRegistry(items)

    async def execute_skill(self, step, inputs, context):
        return await self._invoke(self.skill_handlers.get(step.uses), step, inputs, context)

    async def execute_action(self, step, inputs, context):
        handler = self.action_handlers.get(step.action)
        if handler is not None:
            return await self._invoke(handler, step, inputs, context)
        return await super().execute_action(step, inputs, context)

    async def execute_agent(self, step, inputs, context):
        return await self._invoke(self.agent_handler, step, inputs, context)

    async def execute_eval(self, step, inputs, context):
        if self.eval_handler is not None:
            return await self._invoke(self.eval_handler, step, inputs, context)
        return await super().execute_eval(step, inputs, context)
