from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import os
import socket
from typing import Any, Iterator
from uuid import uuid4

from .errors import LeaseConflict, ReconciliationRequired, SchemaVersionError
from .integrity import compiled_plan_integrity, run_state_integrity
from .telemetry import redact
from .migrations import RUN_STATE_CURRENT, RUN_STATE_MIGRATIONS
from .models import (
    ArtifactRef,
    CompiledPlan,
    Event,
    InvocationStatus,
    ProviderReceipt,
    RunState,
    StepStatus,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    backend_id = "filesystem"

    """Inspectable filesystem store with atomic snapshots and a hash-chained event log.

    State snapshots are compatibility-sensitive. v0.6 reads released v2/v3/v4 snapshots
    through the explicit migration registry and writes v5. A renewable per-run lease
    prevents two runners from mutating the same workflow concurrently.
    """

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "artifacts").mkdir(exist_ok=True)
        self._last_event_hash: str | None = None
        self._event_chain_initialized = False
        self._lease_token: str | None = None

    @property
    def state_path(self):
        return self.run_dir / "state.json"

    @property
    def plan_path(self):
        return self.run_dir / "plan.json"

    @property
    def events_path(self):
        return self.run_dir / "events.jsonl"

    @property
    def lease_path(self):
        return self.run_dir / ".lease.json"

    @property
    def cancel_path(self):
        return self.run_dir / "cancel.request.json"

    def has_state(self) -> bool:
        return self.state_path.exists()

    def source_state_version(self) -> str | None:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("schema_version")
        except Exception:
            return None

    def backup_state_snapshot(self) -> str | None:
        if not self.state_path.exists():
            return None
        raw = self.state_path.read_text(encoding="utf-8")
        version = self.source_state_version() or "unknown"
        safe_version = version.rsplit("/", 1)[-1].replace("/", "-")
        directory = self.run_dir / "migrations"
        directory.mkdir(exist_ok=True)
        path = directory / f"state-{safe_version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        self._atomic_write_text(path, raw)
        return str(path.relative_to(self.run_dir))

    def has_any_run_data(self) -> bool:
        return self.state_path.exists() or self.plan_path.exists() or self.events_path.exists()

    def _atomic_write_text(self, path: Path, text: str):
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    def save_plan(self, plan: CompiledPlan):
        self._atomic_write_text(self.plan_path, plan.model_dump_json(indent=2, by_alias=True))

    def load_plan(self) -> CompiledPlan:
        return CompiledPlan.model_validate_json(self.plan_path.read_text(encoding="utf-8"))

    def save_state(self, state: RunState):
        state.integrity_hash = run_state_integrity(state)
        self._atomic_write_text(self.state_path, state.model_dump_json(indent=2))

    def _migrate_state_dict(self, raw: dict[str, Any]) -> dict[str, Any]:
        return RUN_STATE_MIGRATIONS.migrate(raw, RUN_STATE_CURRENT)

    def load_state(self) -> RunState:
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        migrated = self._migrate_state_dict(raw)
        state = RunState.model_validate(migrated)
        if raw.get("schema_version") != migrated.get("schema_version"):
            state.storage_backend = self.backend_id
            state.integrity_hash = run_state_integrity(state)
        return state

    def append_event(self, event: Event):
        if not self._event_chain_initialized:
            events = self.read_events()
            self._last_event_hash = events[-1].get("event_hash") if events else None
            self._event_chain_initialized = True
        event.prev_hash = self._last_event_hash
        body = event.model_dump(mode="json")
        body["event_hash"] = None
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        event.event_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._last_event_hash = event.event_hash

    def append_system_event(self, state: RunState, event_type: str, *, step_id: str | None = None, **data):
        events = self.read_events()
        seq = max((x.get("seq", 0) for x in events), default=0) + 1
        self.append_event(Event(seq=seq, run_id=state.run_id, type=event_type, step_id=step_id, data=data))

    def read_events(self):
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines() if line]

    def verify_event_chain(self) -> tuple[bool, str | None]:
        previous = None
        expected_seq = 1
        run_id = None
        for event in self.read_events():
            if event.get("seq") != expected_seq:
                return False, f"event seq mismatch: expected={expected_seq} got={event.get('seq')}"
            expected_seq += 1
            if run_id is None:
                run_id = event.get("run_id")
            elif event.get("run_id") != run_id:
                return False, f"run_id mismatch at seq={event.get('seq')}"
            if event.get("prev_hash") != previous:
                return False, f"prev_hash mismatch at seq={event.get('seq')}"
            expected = event.get("event_hash")
            body = dict(event)
            body["event_hash"] = None
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            actual = "sha256:" + hashlib.sha256(canonical).hexdigest()
            if expected != actual:
                return False, f"event_hash mismatch at seq={event.get('seq')}"
            previous = expected
        return True, None

    def verify_plan_integrity(self) -> tuple[bool, str | None]:
        try:
            plan = self.load_plan()
        except Exception as exc:
            return False, f"cannot load plan: {exc}"
        actual = compiled_plan_integrity(plan)
        if actual != plan.integrity_hash:
            return False, "compiled plan integrity_hash mismatch"
        return True, None

    def verify_state_integrity(self) -> tuple[bool, str | None]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"cannot load state: {exc}"
        version = raw.get("schema_version")
        if version not in {
            "agent-playbook-os/run-state/v2",
            "agent-playbook-os/run-state/v3",
            "agent-playbook-os/run-state/v4",
            "agent-playbook-os/run-state/v5",
        }:
            return False, f"unsupported run state schema: {version!r}"
        actual = run_state_integrity(raw)
        if actual != raw.get("integrity_hash"):
            return False, "run state integrity_hash mismatch"
        return True, None

    def request_cancellation(self, reason: str = "requested", actor: str = "human") -> dict[str, Any]:
        payload = {"requested_at": _now(), "reason": reason, "actor": actor}
        self._atomic_write_text(self.cancel_path, json.dumps(payload, indent=2, sort_keys=True))
        return payload

    def cancellation_request(self) -> dict[str, Any] | None:
        if not self.cancel_path.exists():
            return None
        try:
            value = json.loads(self.cancel_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"reason": "requested"}
        except Exception:
            return {"reason": "requested"}

    def clear_cancellation_request(self):
        self.cancel_path.unlink(missing_ok=True)

    def _lease_is_live(self, payload: dict[str, Any]) -> bool:
        heartbeat = payload.get("heartbeat_at") or payload.get("acquired_at")
        ttl = payload.get("ttl_seconds")
        if isinstance(heartbeat, str) and isinstance(ttl, (int, float)) and ttl > 0:
            try:
                ts = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - ts > timedelta(seconds=float(ttl)):
                    return False
            except Exception:
                return True
        if payload.get("host") != socket.gethostname():
            return True  # Remote/unknown host with a fresh heartbeat: fail closed.
        pid = payload.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def acquire_lease(self, owner: str | None = None, *, ttl_seconds: float = 30.0) -> str:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        token = str(uuid4())
        timestamp = _now()
        payload = {
            "token": token,
            "owner": owner or f"pid:{os.getpid()}",
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": timestamp,
            "heartbeat_at": timestamp,
            "ttl_seconds": float(ttl_seconds),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self.lease_path, flags, 0o600)
        except FileExistsError:
            try:
                current = json.loads(self.lease_path.read_text(encoding="utf-8"))
            except Exception:
                current = {"owner": "unknown"}
            if not self._lease_is_live(current):
                self.lease_path.unlink(missing_ok=True)
                return self.acquire_lease(owner, ttl_seconds=ttl_seconds)
            raise LeaseConflict(f"run already leased by {current.get('owner', 'unknown')}")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        self._lease_token = token
        return token

    def renew_lease(self, token: str, *, ttl_seconds: float | None = None) -> dict[str, Any]:
        if not self.lease_path.exists():
            raise LeaseConflict("lease disappeared before renewal")
        try:
            current = json.loads(self.lease_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LeaseConflict(f"cannot read lease during renewal: {exc}") from exc
        if current.get("token") != token:
            raise LeaseConflict("lease token changed before renewal")
        if ttl_seconds is not None:
            if ttl_seconds <= 0:
                raise ValueError("lease ttl_seconds must be positive")
            current["ttl_seconds"] = float(ttl_seconds)
        current["heartbeat_at"] = _now()
        self._atomic_write_text(self.lease_path, json.dumps(current, sort_keys=True))
        return current

    def lease_status(self) -> dict[str, Any] | None:
        if not self.lease_path.exists():
            return None
        current = json.loads(self.lease_path.read_text(encoding="utf-8"))
        return {**current, "live": self._lease_is_live(current)}

    def release_lease(self, token: str | None = None):
        expected = token or self._lease_token
        if not expected or not self.lease_path.exists():
            return
        try:
            current = json.loads(self.lease_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if current.get("token") == expected:
            self.lease_path.unlink(missing_ok=True)
        if self._lease_token == expected:
            self._lease_token = None

    def break_lease(self, *, actor: str = "human") -> dict[str, Any] | None:
        if not self.lease_path.exists():
            return None
        current = json.loads(self.lease_path.read_text(encoding="utf-8"))
        self.lease_path.unlink()
        current["broken_at"] = _now()
        current["broken_by"] = actor
        return current

    @contextmanager
    def lease(self, owner: str | None = None, *, ttl_seconds: float = 30.0) -> Iterator[str]:
        token = self.acquire_lease(owner, ttl_seconds=ttl_seconds)
        try:
            yield token
        finally:
            self.release_lease(token)

    def attach_provider_receipt(
        self,
        invocation_id: str,
        receipt: ProviderReceipt,
        *,
        actor: str = "operator",
    ) -> RunState:
        ok, error = self.verify_state_integrity()
        if not ok:
            raise ReconciliationRequired(f"cannot attach receipt to tampered state: {error}")
        state = self.load_state()
        matches = [x for x in state.invocations if x.invocation_id == invocation_id]
        if len(matches) != 1:
            raise ReconciliationRequired(f"invocation not found: {invocation_id}")
        inv = matches[0]
        receipt = receipt.model_copy(update={"metadata": redact(receipt.metadata)})
        if not any(x.receipt_id == receipt.receipt_id for x in state.provider_receipts):
            state.provider_receipts.append(receipt)
        if receipt.receipt_id not in inv.provider_receipt_ids:
            inv.provider_receipt_ids.append(receipt.receipt_id)
        inv.provider = inv.provider or receipt.provider
        inv.provider_call_id = inv.provider_call_id or receipt.call_id
        target = state.steps.get(inv.step_id) or state.nested_steps.get(inv.step_id)
        if target is not None and receipt.receipt_id not in target.provider_receipt_ids:
            target.provider_receipt_ids.append(receipt.receipt_id)
        self.save_state(state)
        self.append_system_event(
            state,
            "provider.receipt_attached",
            step_id=inv.step_id,
            invocation_id=invocation_id,
            receipt_id=receipt.receipt_id,
            provider=receipt.provider,
            call_id=receipt.call_id,
            status=receipt.status,
            actor=actor,
        )
        return state

    def reconcile_invocation(
        self,
        invocation_id: str,
        *,
        outcome: str,
        actor: str = "human",
        output: Any = None,
        note: str | None = None,
    ) -> RunState:
        if outcome not in {"not-executed", "succeeded"}:
            raise ValueError("outcome must be 'not-executed' or 'succeeded'")
        ok, error = self.verify_state_integrity()
        if not ok:
            raise ReconciliationRequired(f"cannot reconcile tampered state: {error}")
        state = self.load_state()
        matches = [x for x in state.invocations if x.invocation_id == invocation_id]
        if len(matches) != 1:
            raise ReconciliationRequired(f"invocation not found: {invocation_id}")
        inv = matches[0]
        if inv.status not in {InvocationStatus.STARTED, InvocationStatus.UNKNOWN}:
            raise ReconciliationRequired(f"invocation is not uncertain: {inv.status.value}")
        inv.status = InvocationStatus.RECONCILED
        inv.finished_at = _now()
        inv.reconciled_by = actor
        inv.reconciliation_note = note or outcome
        if outcome == "succeeded":
            if output is None:
                raise ReconciliationRequired("succeeded reconciliation requires output")
            inv.result_hash = "sha256:" + hashlib.sha256(
                json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
            ).hexdigest()
            target = state.steps.get(inv.step_id) or state.nested_steps.get(inv.step_id)
            if target is None:
                raise ReconciliationRequired(f"step state not found for invocation: {inv.step_id}")
            target.output = output
            target.status = StepStatus.COMPLETED
            target.error = None
            target.finished_at = _now()
        self.save_state(state)
        self.append_system_event(
            state,
            "invocation.reconciled",
            step_id=inv.step_id,
            invocation_id=invocation_id,
            outcome=outcome,
            actor=actor,
        )
        return state

    def write_artifact(
        self,
        artifact_id: str,
        data: bytes | str | dict[str, Any] | list[Any],
        *,
        produced_by: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactRef:
        if not artifact_id or artifact_id in {".", ".."} or "/" in artifact_id or "\\" in artifact_id:
            raise ValueError("artifact_id must be a single safe path segment")
        if isinstance(data, bytes):
            payload = data
            suffix = ".bin"
        elif isinstance(data, str):
            payload = data.encode("utf-8")
            suffix = ".txt"
            media_type = media_type or "text/plain"
        else:
            payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
            suffix = ".json"
            media_type = media_type or "application/json"
        path = self.run_dir / "artifacts" / f"{artifact_id}{suffix}"
        with path.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        content_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
        return ArtifactRef(
            id=artifact_id,
            uri=f"file:artifacts/{path.name}",
            media_type=media_type,
            content_hash=content_hash,
            produced_by=produced_by,
        )

    def verify_artifacts(self, refs: list[ArtifactRef], verifier_registry=None, tenant_scope=None) -> tuple[bool, list[str]]:
        errors: list[str] = []
        base = self.run_dir.resolve()
        external: list[ArtifactRef] = []
        for ref in refs:
            if not ref.uri.startswith("file:"):
                external.append(ref)
                continue
            if not ref.content_hash:
                errors.append(f"artifact {ref.id} missing content_hash")
                continue
            raw = ref.uri[5:]
            path = (self.run_dir / raw).resolve()
            if not path.is_relative_to(base):
                errors.append(f"artifact {ref.id} escapes run directory")
                continue
            if not path.exists():
                errors.append(f"artifact {ref.id} missing: {raw}")
                continue
            actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != ref.content_hash:
                errors.append(f"artifact {ref.id} hash mismatch")
        if external:
            if verifier_registry is None:
                errors.extend(f"artifact {ref.id} external URI is not verified: {ref.uri}" for ref in external)
            else:
                ok, ext_errors = verifier_registry.verify_refs(
                    external, local_store=None, tenant_scope=tenant_scope, require_all_external=True
                )
                if not ok:
                    errors.extend(ext_errors)
        return not errors, errors
