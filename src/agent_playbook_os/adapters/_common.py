from __future__ import annotations

import asyncio
import inspect
from typing import Any


def maybe_await(value: Any):
    return value


async def await_if_needed(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def invoke_client(call, **kwargs: Any) -> Any:
    """Keep synchronous SDK I/O off the control-plane event loop.

    Cancelling this wait does not stop a thread or retract a provider request.
    Hosts must configure SDK network deadlines; the runner retains UNKNOWN for
    timed-out material operations rather than accepting a late success.
    """
    value = call(**kwargs) if inspect.iscoroutinefunction(call) else await asyncio.to_thread(call, **kwargs)
    return await await_if_needed(value)


def get_value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def objective_text(step, inputs: dict[str, Any]) -> str:
    objective = inputs.get("objective") or step.description or "Complete the requested agent step."
    remaining = {k: v for k, v in inputs.items() if k != "objective"}
    if not remaining:
        return str(objective)
    return f"{objective}\n\nStructured inputs:\n{remaining!r}"


def jsonable_output(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): jsonable_output(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable_output(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    return repr(value)
