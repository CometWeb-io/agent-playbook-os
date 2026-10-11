from __future__ import annotations

from pathlib import Path
import json
import os
from typing import Any
try:
    import fcntl
except ImportError:
    fcntl = None

from .errors import IntegrityError, SideEffectOutcomeUnknown

from .integrity import canonical_hash
from .models import RuntimeResult, RuntimeStreamEvent
from .telemetry import redact
from .secret_resolver import redact_secret_values


def _jsonable(value: Any) -> Any:
    if isinstance(value, RuntimeResult):
        safe = value.model_copy(deep=True)
        safe.value = redact(redact_secret_values(safe.value))
        for receipt in safe.provider_receipts:
            receipt.metadata = redact(redact_secret_values(receipt.metadata))
        return {"__runtime_result__": True, "value": safe.model_dump(mode="json")}
    if isinstance(value, RuntimeStreamEvent):
        safe = value.model_copy(deep=True)
        safe.value = redact(redact_secret_values(safe.value))
        safe.metadata = redact(redact_secret_values(safe.metadata))
        if safe.receipt is not None:
            safe.receipt.metadata = redact(redact_secret_values(safe.receipt.metadata))
        return {"__runtime_stream_event__": True, "value": safe.model_dump(mode="json")}
    value = redact(value)
    if hasattr(value, "model_dump"):
        return redact(value.model_dump(mode="json"))
    try:
        json.dumps(value)
        return value
    except TypeError:
        # repr() may expose credentials in custom adapter objects.
        return {"__unsupported_type__": type(value).__name__}


def _restore(value: Any) -> Any:
    if isinstance(value, dict) and value.get("__runtime_result__") is True:
        return RuntimeResult.model_validate(value["value"])
    if isinstance(value, dict) and value.get("__runtime_stream_event__") is True:
        return RuntimeStreamEvent.model_validate(value["value"])
    return value



DEFAULT_MAX_CASSETTE_BYTES = 128_000_000
DEFAULT_MAX_RECORD_BYTES = 24_000_000
DEFAULT_MAX_RECORDS = 50_000


def _positive_limit(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _last_record(fileobj, *, max_record_bytes: int):
    """Read only the last record, even when an existing cassette is large."""
    fileobj.seek(0, os.SEEK_END)
    cursor = fileobj.tell()
    if cursor == 0:
        return None
    parts = []
    while cursor > 0:
        size = min(65536, cursor)
        cursor -= size
        fileobj.seek(cursor)
        parts.append(fileobj.read(size))
        data = b"".join(reversed(parts)).rstrip(b"\n")
        if b"\n" in data or cursor == 0:
            line = data.rsplit(b"\n", 1)[-1]
            if len(line) > max_record_bytes:
                raise IntegrityError("cassette contains oversized existing record")
            try:
                return json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise IntegrityError("cassette has a corrupted terminal record") from exc
        if len(data) > max_record_bytes:
            raise IntegrityError("cassette contains oversized existing record")
    return None

class RecordingRuntime:
    """Runtime decorator that records deterministic invocation cassettes.

    Context is intentionally not persisted in full; run IDs and potentially sensitive
    state would make cassettes non-portable and leak more data than necessary.
    """

    def __init__(
        self, inner, cassette: str | Path, *,
        max_cassette_bytes: int = DEFAULT_MAX_CASSETTE_BYTES,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        max_stream_events: int = 10_000,
        max_stream_bytes: int = 16_000_000,
    ):
        self.inner = inner
        self.cassette = Path(cassette)
        self.max_cassette_bytes = _positive_limit("max_cassette_bytes", max_cassette_bytes)
        self.max_record_bytes = _positive_limit("max_record_bytes", max_record_bytes)
        self.max_stream_events = _positive_limit("max_stream_events", max_stream_events)
        self.max_stream_bytes = _positive_limit("max_stream_bytes", max_stream_bytes)
        if self.max_record_bytes > self.max_cassette_bytes:
            raise ValueError("max_record_bytes cannot exceed max_cassette_bytes")
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
        # A failed write happens AFTER a tool might have taken effect.
        # Fail as UNKNOWN rather than permitting a blind material retry.
        try:
            with self.cassette.open("a+b") as f:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.seek(0, os.SEEK_END)
                    if f.tell() > self.max_cassette_bytes:
                        raise IntegrityError("cassette exceeds maximum size")
                    previous = _last_record(f, max_record_bytes=self.max_record_bytes)
                    if previous is not None:
                        previous_hash = previous.get("record_hash")
                        if not isinstance(previous_hash, str) or not previous_hash.startswith("sha256:"):
                            raise IntegrityError("cannot append to legacy or unverified cassette")
                        prior_body = {key: value for key, value in previous.items() if key != "record_hash"}
                        if canonical_hash(prior_body) != previous_hash:
                            raise IntegrityError("existing cassette terminal record has invalid hash")
                    else:
                        previous_hash = None
                    record = {**record, "prev_hash": previous_hash}
                    record["record_hash"] = canonical_hash(record)
                    encoded = (
                        json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
                        + b"\n"
                    )
                    if len(encoded) > self.max_record_bytes:
                        raise ValueError("cassette record exceeds maximum size")
                    f.seek(0, os.SEEK_END)
                    if f.tell() + len(encoded) > self.max_cassette_bytes:
                        raise ValueError("cassette exceeds maximum size")
                    f.write(encoded)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    if fcntl is not None:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            raise SideEffectOutcomeUnknown("cassette write could not be committed") from exc

    async def _call(self, kind, method, step, inputs, context):
        safe_inputs = redact(inputs)
        signature = canonical_hash({"kind": kind, "step_id": step.id, "inputs": safe_inputs})
        base = {"kind": kind, "step_id": step.id, "signature": signature, "inputs": _jsonable(safe_inputs)}
        try:
            out = await method(step, inputs, context)
        except Exception as exc:
            self._write({**base, "ok": False, "error_type": type(exc).__name__, "error": "<runtime exception details omitted>"})
            raise
        if hasattr(out, "__aiter__"):
            async def recorded_stream():
                events = []
                stream_bytes = 0
                try:
                    async for item in out:
                        if len(events) >= self.max_stream_events:
                            raise SideEffectOutcomeUnknown("cassette streaming event limit exceeded")
                        event = item if isinstance(item, RuntimeStreamEvent) else RuntimeStreamEvent.model_validate(item)
                        safe = _jsonable(event)["value"]
                        item_bytes = len(json.dumps(safe, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                        stream_bytes += item_bytes
                        if stream_bytes > self.max_stream_bytes:
                            raise SideEffectOutcomeUnknown("cassette streaming byte limit exceeded")
                        events.append(safe)
                        yield event
                except Exception as exc:
                    self._write({**base, "ok": False, "stream": events, "error_type": type(exc).__name__, "error": "<runtime exception details omitted>"})
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

    def __init__(
        self, cassette: str | Path, *,
        max_cassette_bytes: int = DEFAULT_MAX_CASSETTE_BYTES,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        max_records: int = DEFAULT_MAX_RECORDS,
        allow_legacy: bool = False,
    ):
        path = Path(cassette)
        self.max_cassette_bytes = _positive_limit("max_cassette_bytes", max_cassette_bytes)
        self.max_record_bytes = _positive_limit("max_record_bytes", max_record_bytes)
        self.max_records = _positive_limit("max_records", max_records)
        if self.max_record_bytes > self.max_cassette_bytes:
            raise ValueError("max_record_bytes cannot exceed max_cassette_bytes")
        with path.open("rb") as f:
            payload = f.read(self.max_cassette_bytes + 1)
        if len(payload) > self.max_cassette_bytes:
            raise IntegrityError("cassette exceeds maximum size")
        self.records = []
        previous_hash = None
        legacy = None
        for raw in payload.splitlines():
            if not raw.strip():
                continue
            if len(raw) > self.max_record_bytes:
                raise IntegrityError("cassette record exceeds maximum size")
            if len(self.records) >= self.max_records:
                raise IntegrityError("cassette record count exceeds maximum")
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise IntegrityError("cassette contains invalid JSON") from exc
            if not isinstance(record, dict):
                raise IntegrityError("cassette record must be a JSON object")
            hashed = "record_hash" in record or "prev_hash" in record
            if legacy is None:
                legacy = not hashed
            elif legacy == hashed:
                raise IntegrityError("cannot mix hashed and legacy cassette records")
            if not hashed:
                if not allow_legacy:
                    raise IntegrityError("legacy cassette requires explicit allow_legacy=True")
            else:
                received_hash = record.get("record_hash")
                if record.get("prev_hash") != previous_hash:
                    raise IntegrityError("cassette record-chain predecessor mismatch")
                body = {k: v for k, v in record.items() if k != "record_hash"}
                if canonical_hash(body) != received_hash:
                    raise IntegrityError("cassette record hash mismatch")
                previous_hash = received_hash
            self.records.append(record)
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
        signature = canonical_hash({"kind": kind, "step_id": step.id, "inputs": redact(inputs)})
        if record.get("kind") != kind or record.get("step_id") != step.id or record.get("signature") != signature:
            raise RuntimeError(
                f"replay divergence at index={self.index}: expected {record.get('kind')}:{record.get('step_id')}"
            )
        self.index += 1
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
