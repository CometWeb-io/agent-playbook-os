from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from .errors import IntegrityError


class SchemaResolver(Protocol):
    def resolve(self, ref: str, expected_hash: str) -> dict[str, Any]: ...


def _normalize_hash(value: str) -> str:
    return value if value.startswith("sha256:") else "sha256:" + value


def verify_schema_bytes(payload: bytes, expected_hash: str) -> dict[str, Any]:
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual.lower() != _normalize_hash(expected_hash).lower():
        raise IntegrityError(f"schema hash mismatch: expected={_normalize_hash(expected_hash)} actual={actual}")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("resolved JSON Schema must be an object")
    return value


class MappingSchemaResolver:
    """Deterministic resolver for tests/hosts that already possess schema bytes."""

    def __init__(self, mapping: dict[str, bytes | str | dict[str, Any]]):
        self.mapping = mapping

    def resolve(self, ref: str, expected_hash: str) -> dict[str, Any]:
        if ref not in self.mapping:
            raise KeyError(f"schema not available: {ref}")
        raw = self.mapping[ref]
        if isinstance(raw, dict):
            payload = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        elif isinstance(raw, str):
            payload = raw.encode()
        else:
            payload = raw
        return verify_schema_bytes(payload, expected_hash)
