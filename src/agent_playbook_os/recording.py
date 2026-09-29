from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from .integrity import canonical_hash
from .models import RuntimeResult, RuntimeStreamEvent
from .telemetry import redact


def _jsonable(value: Any) -> Any:
    if isinstance(value, RuntimeResult):
        return {"__runtime_result__": True, "value": redact(value.model_dump(mode="json"))}
    if isinstance(value, RuntimeStreamEvent):
        return {"__runtime_stream_event__": True, "value": redact(value.model_dump(mode="json"))}
    value = redact(value)
    if hasattr(value, "model_dump"):
        return redact(value.model_dump(mode="json"))
    try:
        json.dumps(value)
        return value
    except TypeError:
        return {"__repr__": repr(value)}


def _restore(value: Any) -> Any:
    if isinstance(value, dict) and value.get("__runtime_result__") is True:
        return RuntimeResult.model_validate(value["value"])
    if isinstance(value, dict) and value.get("__runtime_stream_event__") is True:
        return RuntimeStreamEvent.model_validate(value["value"])
    return value


class RecordingRuntime:
    """Runtime decorator that records deterministic invocation cassettes.

    Context is intentionally not persisted in full; run IDs and potentially sensitive
    state would make cassettes non-portable and leak more data than necessary.
    """

    def __init__(self, inner, cassette: str | Path):
        self.inner = inner
        self.cassette = Path(cassette)
        self.cassette.parent.mkdir(parents=True, exist_ok=True)

    def capabilities(self):
        return self.inner.capabilities() if hasattr(self.inner, "capabilities") else None

    def supports_isolation(self, mode, step=None):
        fn = getattr(self.inner, "supports_isolation", None)
        return fn(mode, step) if fn else mode in {"inline", "auto"}

    def supports_idempotency(self, step):
        fn = getattr(self.inner, "supports_idempotency", None)
        return bool(fn(step)) if fn else False

    def _write(self, record):
        with self.cassette.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, ensure_ascii=False, default=str) + "\n")

    async def _call(self, kind, method, step, inputs, context):
        safe_inputs = redact(inputs)
        signature = canonical_hash({"kind": kind, "step_id": step.id, "inputs": safe_inputs})
        base = {"kind": kind, "step_id": step.id, "signature": signature, "inputs": _jsonable(safe_inputs)}
        try:
            out = await method(step, inputs, context)
        except Exception as exc:
            self._write({**base, "ok": False, "error_type": type(exc).__name__, "error": str(exc)})
            raise
        if hasattr(out, "__aiter__"):
            async def recorded_stream():
                events = []
                try:
                    async for item in out:
                        event = item if isinstance(item, RuntimeStreamEvent) else RuntimeStreamEvent.model_validate(item)
                        events.append(event.model_dump(mode="json"))
                        yield event
                except Exception as exc:
                    self._write({**base, "ok": False, "stream": events, "error_type": type(exc).__name__, "error": str(exc)})
                    raise
                self._write({**base, "ok": True, "stream": events})
            return recorded_stream()
        self._write({**base, "ok": True, "output": _jsonable(out)})
        return out

    async def execute_skill(self, step, inputs, context):
        return await self._call("skill", self.inner.execute_skill, step, inputs, context)

    async def execute_action(self, step, inputs, context):
        return await self._call("action", self.inner.execute_action, step, inputs, context)

    async def execute_agent(self, step, inputs, context):
        return await self._call("agent", self.inner.execute_agent, step, inputs, context)

    async def execute_eval(self, step, inputs, context):
        return await self._call("eval", self.inner.execute_eval, step, inputs, context)


class ReplayRuntime:
    """Strict cassette runtime. Invocation order and signatures must match exactly."""

    def __init__(self, cassette: str | Path):
        path = Path(cassette)
        self.records = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.index = 0

    def capabilities(self):
        from .capabilities import CapabilityDescriptor, CapabilityRegistry
        return CapabilityRegistry([
            CapabilityDescriptor(
                id="runtime:replay",
                kind="trace",
                description="Strict deterministic invocation replay from a recorded cassette.",
                supports_dry_run=True,
            )
        ])

    def supports_isolation(self, mode, step=None):
        return True

    def supports_idempotency(self, step):
        return True

    async def _next(self, kind, step, inputs):
        if self.index >= len(self.records):
            raise RuntimeError("replay cassette exhausted")
        record = self.records[self.index]
        self.index += 1
        signature = canonical_hash({"kind": kind, "step_id": step.id, "inputs": redact(inputs)})
        if record.get("kind") != kind or record.get("step_id") != step.id or record.get("signature") != signature:
            raise RuntimeError(
                f"replay divergence at index={self.index - 1}: expected {record.get('kind')}:{record.get('step_id')}"
            )
        if not record.get("ok"):
            raise RuntimeError(f"recorded {record.get('error_type')}: {record.get('error')}")
        if "stream" in record:
            async def replay_stream():
                for item in record.get("stream", []):
                    yield RuntimeStreamEvent.model_validate(item)
            return replay_stream()
        return _restore(record.get("output"))

    async def execute_skill(self, step, inputs, context):
        return await self._next("skill", step, inputs)

    async def execute_action(self, step, inputs, context):
        return await self._next("action", step, inputs)

    async def execute_agent(self, step, inputs, context):
        return await self._next("agent", step, inputs)

    async def execute_eval(self, step, inputs, context):
        return await self._next("eval", step, inputs)
