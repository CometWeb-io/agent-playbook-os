from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def compiled_plan_integrity(plan_or_dict: Any) -> str:
    if hasattr(plan_or_dict, "model_dump"):
        data = plan_or_dict.model_dump(mode="json", by_alias=True)
    else:
        data = dict(plan_or_dict)
    data = dict(data)
    data.pop("integrity_hash", None)
    return canonical_hash(data)


def run_state_integrity(state_or_dict: Any) -> str:
    if hasattr(state_or_dict, "model_dump"):
        data = state_or_dict.model_dump(mode="json")
    else:
        data = dict(state_or_dict)
    data = dict(data)
    data.pop("integrity_hash", None)
    return canonical_hash(data)


def compiled_plan_semantic_hash(plan_or_dict: Any) -> str:
    """Stable fingerprint of executable semantics, excluding host-local/run-local metadata."""
    if hasattr(plan_or_dict, "model_dump"):
        data = plan_or_dict.model_dump(mode="json", by_alias=True)
    else:
        data = dict(plan_or_dict)
    data = dict(data)
    for key in ("integrity_hash", "semantic_hash", "compiled_at", "source_path"):
        data.pop(key, None)
    locks = data.get("skill_lock", {})
    normalized_locks = {}
    for sid, entry in locks.items():
        entry = dict(entry)
        entry.pop("path", None)
        entry.pop("resolved", None)
        normalized_locks[sid] = entry
    data["skill_lock"] = normalized_locks
    return canonical_hash(data)
