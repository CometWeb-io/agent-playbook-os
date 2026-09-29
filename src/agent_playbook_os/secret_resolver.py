from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from .errors import SecretResolutionError


class SecretValue(str):
    """String-compatible secret marker that observability layers can redact."""

    def __new__(cls, value: str, source_ref: str | None = None):
        obj = super().__new__(cls, value)
        obj.source_ref = source_ref
        return obj


class SecretResolver(Protocol):
    def supports(self, ref: str) -> bool: ...
    def resolve(self, ref: str) -> SecretValue: ...


class EnvSecretResolver:
    def supports(self, ref: str) -> bool:
        return ref.startswith("env://")

    def resolve(self, ref: str) -> SecretValue:
        if not self.supports(ref):
            raise SecretResolutionError(f"unsupported secret reference: {ref}")
        name = ref[6:]
        if not name or any(x in name for x in "\x00\r\n"):
            raise SecretResolutionError("invalid env secret reference")
        value = os.environ.get(name)
        if value is None:
            raise SecretResolutionError(f"secret environment variable is not set: {name}")
        return SecretValue(value, ref)


class MappingSecretResolver:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    def supports(self, ref: str) -> bool:
        return ref.startswith("secret://")

    def resolve(self, ref: str) -> SecretValue:
        if not self.supports(ref):
            raise SecretResolutionError(f"unsupported secret reference: {ref}")
        key = ref[9:]
        if key not in self.values:
            raise SecretResolutionError(f"secret not found: {key}")
        return SecretValue(str(self.values[key]), ref)


class FileSecretResolver:
    def __init__(self, roots: list[str | Path] | None = None, *, max_bytes: int = 64 * 1024):
        self.roots = [Path(x).expanduser().resolve() for x in (roots or [])]
        self.max_bytes = max_bytes

    def supports(self, ref: str) -> bool:
        return ref.startswith("file-secret://")

    def resolve(self, ref: str) -> SecretValue:
        if not self.supports(ref):
            raise SecretResolutionError(f"unsupported secret reference: {ref}")
        raw = ref[len("file-secret://"):]
        if not raw:
            raise SecretResolutionError("empty file secret reference")
        if not self.roots:
            raise SecretResolutionError("file secret resolver has no configured roots")
        requested = Path(raw).expanduser()
        if requested.is_absolute():
            path = requested.resolve()
            if not any(path.is_relative_to(root) for root in self.roots):
                raise SecretResolutionError(f"file secret is outside configured roots: {path}")
        else:
            candidates = []
            for root in self.roots:
                candidate = (root / requested).resolve()
                if not candidate.is_relative_to(root):
                    raise SecretResolutionError(f"file secret path escapes configured root: {raw}")
                if candidate.exists():
                    candidates.append(candidate)
            if len(candidates) > 1:
                raise SecretResolutionError(f"ambiguous file secret reference across configured roots: {raw}")
            if not candidates:
                raise SecretResolutionError(f"file secret not found in configured roots: {raw}")
            path = candidates[0]
        try:
            stat = path.stat()
        except OSError as exc:
            raise SecretResolutionError(f"cannot read file secret: {path}: {exc}") from exc
        if stat.st_size > self.max_bytes:
            raise SecretResolutionError(f"file secret exceeds max_bytes={self.max_bytes}")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
        return SecretValue(value, ref)


class SecretResolverChain:
    def __init__(self, resolvers: list[SecretResolver] | None = None):
        self.resolvers = list(resolvers or [])

    def add(self, resolver: SecretResolver) -> None:
        self.resolvers.append(resolver)

    def resolve(self, ref: str) -> SecretValue:
        for resolver in self.resolvers:
            if resolver.supports(ref):
                return resolver.resolve(ref)
        raise SecretResolutionError(f"no resolver configured for secret reference: {ref}")


def is_secret_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("env://", "secret://", "file-secret://"))


def resolve_secret_references(value: Any, resolver: SecretResolverChain | SecretResolver | None) -> Any:
    if is_secret_reference(value):
        if resolver is None:
            raise SecretResolutionError(f"secret reference requires a resolver: {value}")
        if hasattr(resolver, "resolve"):
            return resolver.resolve(value)  # type: ignore[union-attr]
    if isinstance(value, dict):
        return {k: resolve_secret_references(v, resolver) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secret_references(v, resolver) for v in value]
    if isinstance(value, tuple):
        return tuple(resolve_secret_references(v, resolver) for v in value)
    return value


def redact_secret_values(value: Any) -> Any:
    """Remove SecretValue instances before durable persistence or observability."""
    if isinstance(value, SecretValue):
        return "<redacted:secret>"
    if isinstance(value, dict):
        return {k: redact_secret_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secret_values(v) for v in value]
    if isinstance(value, tuple):
        return [redact_secret_values(v) for v in value]
    return value
