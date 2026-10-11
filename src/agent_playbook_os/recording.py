from __future__ import annotations

from pathlib import Path
import json
import os
import hmac
import hashlib
from typing import Any
try:
    import fcntl
except ImportError:
    fcntl = None

from .errors import IntegrityError, SecretResolutionError, SideEffectOutcomeUnknown

from .integrity import canonical_hash
from .models import RuntimeResult, RuntimeStreamEvent
from .telemetry import redact
from .secret_resolver import SecretValue, redact_secret_values


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


_KEYED_INPUT_SCHEME = "hmac-sha256:inputs-v1"
_REDACTED_INPUT_SCHEME = "sha256:redacted-v1"
_INPUT_CONTEXT = b"agent-playbook-os/input-fingerprint/v1\\x00"


def _contains_secret(value: Any) -> bool:
    if isinstance(value, SecretValue):
        return True
    if type(value) is dict:
        return any(_contains_secret(key) or _contains_secret(item) for key, item in value.items())
    if type(value) in {list, tuple}:
        return any(_contains_secret(item) for item in value)
    return False


def _input_fingerprint_value(value: Any) -> Any:
    # A keyed digest can safely bind the actual secret bytes while keeping
    # them out of durable recordings. Include source identity as well.
    if isinstance(value, SecretValue):
        return {"__secret_value__": str(value), "__source_ref__": value.source_ref}
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise SecretResolutionError("cassette input keys must be strings")
        return {key: _input_fingerprint_value(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_input_fingerprint_value(item) for item in value]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise SecretResolutionError("cassette cannot fingerprint unsupported input type")


def _input_signature(kind: str, step_id: str, inputs: Any, integrity_key: bytes | None, *, scheme: str):
    if scheme == _REDACTED_INPUT_SCHEME:
        if _contains_secret(inputs):
            raise SecretResolutionError(
                "secret-bearing cassette invocation requires integrity_key; "
                "redacted inputs cannot distinguish different secret values"
            )
        return canonical_hash({"kind": kind, "step_id": step_id, "inputs": redact(inputs)})
    if scheme == _KEYED_INPUT_SCHEME:
        if integrity_key is None:
            raise SecretResolutionError("HMAC cassette input signatures require an integrity_key")
        raw = json.dumps(
            {
                "kind": kind, "step_id": step_id,
                "inputs": _input_fingerprint_value(inputs),
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        mac = hmac.new(
            integrity_key, _INPUT_CONTEXT + raw, hashlib.sha256
        ).hexdigest()
        return "hmac-sha256:" + mac
    raise IntegrityError("unknown cassette input signature scheme")


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
        integrity_key: bytes | None = None,
    ):
        if integrity_key is not None and (type(integrity_key) is not bytes or len(integrity_key) < 32):
            raise ValueError("integrity_key must be at least 32 bytes")
        self.integrity_key = integrity_key
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
                        prior_body = {
                            key: value for key, value in previous.items()
                            if key not in {"record_hash", "record_mac"}
                        }
                        if canonical_hash(prior_body) != previous_hash:
                            raise IntegrityError("existing cassette terminal record has invalid hash")
                        prior_mac = previous.get("record_mac")
                        if self.integrity_key is None and prior_mac is not None:
                            raise IntegrityError("authenticated cassette requires an integrity_key")
                        if self.integrity_key is not None:
                            expected_mac = hmac.new(
                                self.integrity_key, previous_hash.encode("ascii"), hashlib.sha256
                            ).hexdigest()
                            if not isinstance(prior_mac, str) or not hmac.compare_digest(
                                prior_mac, "hmac-sha256:" + expected_mac
                            ):
                                raise IntegrityError("existing cassette authentication mismatch")
                    else:
                        previous_hash = None
                    record = {**record, "prev_hash": previous_hash}
                    record["record_hash"] = canonical_hash(record)
                    if self.integrity_key is not None:
                        digest = hmac.new(
                            self.integrity_key, record["record_hash"].encode("ascii"), hashlib.sha256
                        ).hexdigest()
                        record["record_mac"] = "hmac-sha256:" + digest
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
        # Reject unsafe redacted-secret collisions BEFORE calling the provider.
        scheme = _KEYED_INPUT_SCHEME if self.integrity_key is not None else _REDACTED_INPUT_SCHEME
        signature = _input_signature(kind, step.id, inputs, self.integrity_key, scheme=scheme)
        safe_inputs = redact(inputs)
        base = {
            "kind": kind, "step_id": step.id, "signature": signature,
            "signature_scheme": scheme, "inputs": _jsonable(safe_inputs),
        }
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
        integrity_key: bytes | None = None,
    ):
        if integrity_key is not None and (type(integrity_key) is not bytes or len(integrity_key) < 32):
            raise ValueError("integrity_key must be at least 32 bytes")
        self.integrity_key = integrity_key
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
                if self.integrity_key is not None:
                    raise IntegrityError("authenticated replay cannot accept unauthenticated legacy records")
                if not allow_legacy:
                    raise IntegrityError("legacy cassette requires explicit allow_legacy=True")
            else:
                received_hash = record.get("record_hash")
                if record.get("prev_hash") != previous_hash:
                    raise IntegrityError("cassette record-chain predecessor mismatch")
                body = {k: v for k, v in record.items() if k not in {"record_hash", "record_mac"}}
                if canonical_hash(body) != received_hash:
                    raise IntegrityError("cassette record hash mismatch")
                received_mac = record.get("record_mac")
                if received_mac is not None and self.integrity_key is None:
                    raise IntegrityError("authenticated cassette requires an integrity_key")
                if self.integrity_key is not None:
                    expected_mac = "hmac-sha256:" + hmac.new(
                        self.integrity_key, received_hash.encode("ascii"), hashlib.sha256
                    ).hexdigest()
                    if not isinstance(received_mac, str) or not hmac.compare_digest(
                        expected_mac, received_mac
                    ):
                        raise IntegrityError("cassette authentication mismatch")
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
        # Legacy recordings did not identify their signature scheme. Permit
        # those only for nonsecret inputs. Secret-bearing legacy replay is
        # ambiguous because multiple distinct credentials redact identically.
        scheme = record.get("signature_scheme", _REDACTED_INPUT_SCHEME)
        signature = _input_signature(
            kind, step.id, inputs, self.integrity_key, scheme=scheme
        )
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
