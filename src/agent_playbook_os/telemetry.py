from __future__ import annotations

from pathlib import Path
import json
import re
from typing import Any, Protocol

_SENSITIVE_KEY = re.compile(r"(?:secret|password|passwd|token|api[_-]?key|authorization|cookie|credential)", re.I)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*\b")


def redact(value: Any, *, max_string: int = 20_000, depth: int = 0) -> Any:
    """Best-effort telemetry/event redaction. It is not a secret vault.

    The function deliberately favors omission over preserving suspicious values.
    It is applied to observability payloads, not to executable inputs.
    """
    if depth > 20:
        return "<redacted:depth>"
    if type(value).__name__ == "SecretValue" or hasattr(value, "source_ref"):
        return "<redacted:secret>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                out[str(key)] = "<redacted>"
            else:
                out[str(key)] = redact(item, max_string=max_string, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [redact(x, max_string=max_string, depth=depth + 1) for x in value]
    if isinstance(value, tuple):
        return [redact(x, max_string=max_string, depth=depth + 1) for x in value]
    if isinstance(value, str):
        text = _BEARER.sub("Bearer <redacted>", value)
        if len(text) > max_string:
            return text[:max_string] + "<truncated>"
        return text
    return value


class TelemetrySink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class NullTelemetrySink:
    def emit(self, event: dict[str, Any]) -> None:
        return None


class JsonlTelemetrySink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redact(event), sort_keys=True, ensure_ascii=False, default=str) + "\n")


class CompositeTelemetrySink:
    """Fan out telemetry while isolating exporter failures from siblings."""

    def __init__(self, sinks):
        self.sinks = list(sinks)

    def emit(self, event: dict[str, Any]) -> None:
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception:
                continue


class OpenTelemetrySink:
    """Optional OpenTelemetry adapter with no hard dependency in core.

    The adapter creates event spans. Run/step correlation is encoded as attributes so
    collectors can reconstruct timelines even when the embedding host owns the parent
    trace context. Instantiation fails clearly when opentelemetry-api is unavailable.
    """

    def __init__(self, tracer=None, *, instrumentation_name: str = "agent-playbook-os"):
        if tracer is None:
            try:
                from opentelemetry import trace
            except ImportError as exc:
                raise RuntimeError("OpenTelemetrySink requires optional package opentelemetry-api") from exc
            tracer = trace.get_tracer(instrumentation_name)
        self.tracer = tracer

    def emit(self, event: dict[str, Any]) -> None:
        safe = redact(event)
        event_type = str(safe.get("type", "event"))
        with self.tracer.start_as_current_span(f"agent-playbook-os.{event_type}") as span:
            for key in ("run_id", "step_id", "seq", "ts"):
                value = safe.get(key)
                if value is not None:
                    span.set_attribute(f"playbook.{key}", str(value))
            data = safe.get("data") or {}
            for key, value in data.items():
                if isinstance(value, (str, int, float, bool)):
                    span.set_attribute(f"playbook.data.{key}", value)
                elif value is not None:
                    span.set_attribute(f"playbook.data.{key}", json.dumps(value, ensure_ascii=False, default=str)[:4096])
