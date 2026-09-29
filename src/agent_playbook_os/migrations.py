from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Callable

from .errors import SchemaVersionError

MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]


class MigrationRegistry:
    """Small explicit migration graph for compatibility-sensitive persisted schemas."""

    def __init__(self):
        self._edges: dict[str, list[tuple[str, MigrationFn]]] = defaultdict(list)

    def register(self, source: str, target: str, fn: MigrationFn) -> None:
        if source == target:
            raise ValueError("migration source and target must differ")
        if self._edges.get(source):
            existing = [x[0] for x in self._edges[source]]
            raise ValueError(f"ambiguous migration source {source!r}; existing targets={existing}")
        self._edges[source].append((target, fn))

    def migrate(self, raw: dict[str, Any], target: str) -> dict[str, Any]:
        source = raw.get("schema_version")
        if source == target:
            return raw
        if not isinstance(source, str):
            raise SchemaVersionError("persisted state is missing schema_version")

        queue = deque([(source, [])])
        seen = {source}
        path: list[tuple[str, str, MigrationFn]] | None = None
        while queue:
            current, steps = queue.popleft()
            for nxt, fn in self._edges.get(current, []):
                candidate = [*steps, (current, nxt, fn)]
                if nxt == target:
                    path = candidate
                    queue.clear()
                    break
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, candidate))
            if path is not None:
                break
        if path is None:
            raise SchemaVersionError(f"no migration path from {source!r} to {target!r}")

        out = dict(raw)
        for expected_source, expected_target, fn in path:
            if out.get("schema_version") != expected_source:
                raise SchemaVersionError(
                    f"migration invariant failed: expected {expected_source!r}, got {out.get('schema_version')!r}"
                )
            out = fn(dict(out))
            out["schema_version"] = expected_target
            # The old integrity hash covered the pre-migration shape. A caller that
            # needs tamper evidence must verify the raw snapshot before migration.
            out["integrity_hash"] = "pending"
        return out


def _v2_to_v3(raw: dict[str, Any]) -> dict[str, Any]:
    raw.setdefault("nested_steps", {})
    raw.setdefault("invocations", [])
    raw.setdefault("cancellation_reason", None)
    raw.setdefault("cancelled_at", None)
    return raw


def _v3_to_v4(raw: dict[str, Any]) -> dict[str, Any]:
    raw.setdefault("provider_receipts", [])
    raw.setdefault("runtime_capabilities_hash", None)
    raw.setdefault("storage_backend", "filesystem")
    for step in raw.get("steps", {}).values():
        if isinstance(step, dict):
            step.setdefault("provider_receipt_ids", [])
    for step in raw.get("nested_steps", {}).values():
        if isinstance(step, dict):
            step.setdefault("provider_receipt_ids", [])
    for inv in raw.get("invocations", []):
        if isinstance(inv, dict):
            inv.setdefault("provider_receipt_ids", [])
            inv.setdefault("provider_call_id", None)
            inv.setdefault("provider", None)
    return raw



def _v4_to_v5(raw: dict[str, Any]) -> dict[str, Any]:
    raw.setdefault("parent_run_id", None)
    if not raw.get("root_run_id"):
        raw["root_run_id"] = raw.get("parent_run_id") or raw.get("run_id")
    raw.setdefault("lineage_depth", 0)
    raw.setdefault("fork_reason", None)
    return raw

RUN_STATE_MIGRATIONS = MigrationRegistry()
RUN_STATE_MIGRATIONS.register("agent-playbook-os/run-state/v2", "agent-playbook-os/run-state/v3", _v2_to_v3)
RUN_STATE_MIGRATIONS.register("agent-playbook-os/run-state/v3", "agent-playbook-os/run-state/v4", _v3_to_v4)
RUN_STATE_MIGRATIONS.register("agent-playbook-os/run-state/v4", "agent-playbook-os/run-state/v5", _v4_to_v5)
RUN_STATE_CURRENT = "agent-playbook-os/run-state/v5"
