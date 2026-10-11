from __future__ import annotations

import hashlib
import json
from referencing import Registry
from referencing.exceptions import NoSuchResource
from jsonschema import validators
from typing import Any, Protocol

from .errors import IntegrityError


MAX_SCHEMA_BYTES = 2_000_000
MAX_SCHEMA_NODES = 20_000
MAX_SCHEMA_DEPTH = 80


def _deny_external_schema_ref(uri: str):
    # Never fetch schemas implicitly, even when the validator library grows
    # support for new URI schemes or a host supplies network handlers.
    raise NoSuchResource(ref=uri)


def validate_output_offline(output: Any, schema: dict[str, Any]) -> None:
    """Validate a result against a bounded, self-contained JSON Schema.

    The schema may use internal JSON Pointers and anchors. External $refs
    (including file://, http(s)://, relative files and dynamic references)
    must be resolved and pinned explicitly by the host before compilation,
    never at runtime through the validator.
    """
    if not isinstance(schema, dict):
        raise ValueError("output JSON Schema must be an object")
    encoded = json.dumps(
        schema, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ValueError("output JSON Schema is too large")
    visited = 0
    stack: list[tuple[Any, int]] = [(schema, 0)]
    while stack:
        node, depth = stack.pop()
        visited += 1
        if visited > MAX_SCHEMA_NODES or depth > MAX_SCHEMA_DEPTH:
            raise ValueError("output JSON Schema exceeds structural limits")
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    if not isinstance(value, str) or not value.startswith("#"):
                        raise ValueError(f"external JSON Schema reference denied: {key}")
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node)

    cls = validators.validator_for(schema)
    cls.check_schema(schema)
    cls(schema, registry=Registry(retrieve=_deny_external_schema_ref)).validate(output)


class SchemaResolver(Protocol):
    def resolve(self, ref: str, expected_hash: str) -> dict[str, Any]: ...


def _normalize_hash(value: str) -> str:
    return value if value.startswith("sha256:") else "sha256:" + value


def verify_schema_bytes(payload: bytes, expected_hash: str) -> dict[str, Any]:
    if len(payload) > MAX_SCHEMA_BYTES:
        raise ValueError("resolved JSON Schema exceeds 2 MB")
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
