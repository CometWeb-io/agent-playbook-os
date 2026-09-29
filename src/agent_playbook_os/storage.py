from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import socket
import sqlite3
from typing import Any, Iterator, Protocol, runtime_checkable
from uuid import uuid4

from .errors import LeaseConflict
from .integrity import run_state_integrity
from .migrations import RUN_STATE_CURRENT, RUN_STATE_MIGRATIONS
from .models import ArtifactRef, CompiledPlan, Event, ProviderReceipt, RunState
from .store import RunStore, _now


@runtime_checkable
class RunStoreProtocol(Protocol):
    run_dir: Path
    backend_id: str

    def has_state(self) -> bool: ...
    def source_state_version(self) -> str | None: ...
    def backup_state_snapshot(self) -> str | None: ...
    def has_any_run_data(self) -> bool: ...
    def save_plan(self, plan: CompiledPlan) -> None: ...
    def load_plan(self) -> CompiledPlan: ...
    def save_state(self, state: RunState) -> None: ...
    def load_state(self) -> RunState: ...
    def append_event(self, event: Event) -> None: ...
    def append_system_event(self, state: RunState, event_type: str, *, step_id: str | None = None, **data: Any) -> None: ...
    def read_events(self) -> list[dict[str, Any]]: ...
    def verify_event_chain(self) -> tuple[bool, str | None]: ...
    def verify_plan_integrity(self) -> tuple[bool, str | None]: ...
    def verify_state_integrity(self) -> tuple[bool, str | None]: ...
    def request_cancellation(self, reason: str = "requested", actor: str = "human") -> dict[str, Any]: ...
    def cancellation_request(self) -> dict[str, Any] | None: ...
    def clear_cancellation_request(self) -> None: ...
    def acquire_lease(self, owner: str | None = None, *, ttl_seconds: float = 30.0) -> str: ...
    def renew_lease(self, token: str, *, ttl_seconds: float | None = None) -> dict[str, Any]: ...
    def release_lease(self, token: str | None = None) -> None: ...
    def lease_status(self) -> dict[str, Any] | None: ...
    def break_lease(self, *, actor: str = "human") -> dict[str, Any] | None: ...
    def attach_provider_receipt(self, invocation_id: str, receipt: ProviderReceipt, *, actor: str = "operator") -> RunState: ...
    def reconcile_invocation(
        self, invocation_id: str, *, outcome: str, actor: str = "human", output: Any = None, note: str | None = None
    ) -> RunState: ...
    def write_artifact(
        self, artifact_id: str, data: bytes | str | dict[str, Any] | list[Any], *, produced_by: str | None = None, media_type: str | None = None
    ) -> ArtifactRef: ...
    def verify_artifacts(self, refs: list[ArtifactRef], verifier_registry=None, tenant_scope=None) -> tuple[bool, list[str]]: ...


class SQLiteRunStore(RunStore):
    """SQLite-backed snapshots/events/lease with filesystem artifacts.

    The database lives inside the run directory so the reference implementation stays
    inspectable and archive-friendly. SQLite transactions provide a stronger local
    durability boundary than independent JSON snapshot files while preserving the same
    RunStore contract used by the engine.
    """

    backend_id = "sqlite"

    def __init__(self, run_dir: str | Path):
        super().__init__(run_dir)
        self.db_path = self.run_dir / "run.sqlite3"
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        return con

    def _get(self, key: str) -> str | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM kv WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else None

    def _set(self, key: str, payload: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO kv(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (key, payload),
            )
            con.commit()

    def _delete(self, key: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM kv WHERE key=?", (key,))
            con.commit()

    def has_state(self) -> bool:
        return self._get("state") is not None

    def source_state_version(self) -> str | None:
        payload = self._get("state")
        if payload is None:
            return None
        try:
            return json.loads(payload).get("schema_version")
        except Exception:
            return None

    def backup_state_snapshot(self) -> str | None:
        payload = self._get("state")
        if payload is None:
            return None
        key = "state-backup:" + _now()
        self._set(key, payload)
        return f"sqlite:{key}"

    def has_any_run_data(self) -> bool:
        with self._connect() as con:
            kv = con.execute("SELECT 1 FROM kv WHERE key IN ('state','plan') LIMIT 1").fetchone()
            ev = con.execute("SELECT 1 FROM events LIMIT 1").fetchone()
            return bool(kv or ev)

    def save_plan(self, plan: CompiledPlan):
        self._set("plan", plan.model_dump_json(indent=2, by_alias=True))

    def load_plan(self) -> CompiledPlan:
        payload = self._get("plan")
        if payload is None:
            raise FileNotFoundError("compiled plan is not present in SQLite store")
        return CompiledPlan.model_validate_json(payload)

    def save_state(self, state: RunState):
        state.integrity_hash = run_state_integrity(state)
        self._set("state", state.model_dump_json(indent=2))

    def load_state(self) -> RunState:
        payload = self._get("state")
        if payload is None:
            raise FileNotFoundError("run state is not present in SQLite store")
        raw = json.loads(payload)
        migrated = RUN_STATE_MIGRATIONS.migrate(raw, RUN_STATE_CURRENT)
        state = RunState.model_validate(migrated)
        if raw.get("schema_version") != migrated.get("schema_version"):
            state.storage_backend = self.backend_id
            state.integrity_hash = run_state_integrity(state)
        return state

    def append_event(self, event: Event):
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            previous = json.loads(row[0]).get("event_hash") if row else None
            event.prev_hash = previous
            body = event.model_dump(mode="json")
            body["event_hash"] = None
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            event.event_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
            con.execute("INSERT INTO events(seq,payload) VALUES(?,?)", (event.seq, event.model_dump_json()))
            con.commit()

    def read_events(self):
        with self._connect() as con:
            return [json.loads(row[0]) for row in con.execute("SELECT payload FROM events ORDER BY seq")]

    def verify_state_integrity(self) -> tuple[bool, str | None]:
        payload = self._get("state")
        if payload is None:
            return False, "cannot load state: missing"
        try:
            raw = json.loads(payload)
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
        return (actual == raw.get("integrity_hash"), None if actual == raw.get("integrity_hash") else "run state integrity_hash mismatch")

    def request_cancellation(self, reason: str = "requested", actor: str = "human") -> dict[str, Any]:
        payload = {"requested_at": _now(), "reason": reason, "actor": actor}
        self._set("cancel", json.dumps(payload, sort_keys=True))
        return payload

    def cancellation_request(self) -> dict[str, Any] | None:
        payload = self._get("cancel")
        return json.loads(payload) if payload else None

    def clear_cancellation_request(self):
        self._delete("cancel")

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
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM kv WHERE key='lease'").fetchone()
            if row:
                current = json.loads(row[0])
                if self._lease_is_live(current):
                    con.rollback()
                    raise LeaseConflict(f"run already leased by {current.get('owner', 'unknown')}")
                con.execute("DELETE FROM kv WHERE key='lease'")
            con.execute("INSERT INTO kv(key,payload) VALUES('lease',?)", (json.dumps(payload, sort_keys=True),))
            con.commit()
        self._lease_token = token
        return token

    def renew_lease(self, token: str, *, ttl_seconds: float | None = None) -> dict[str, Any]:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM kv WHERE key='lease'").fetchone()
            if not row:
                con.rollback()
                raise LeaseConflict("lease disappeared before renewal")
            current = json.loads(row[0])
            if current.get("token") != token:
                con.rollback()
                raise LeaseConflict("lease token changed before renewal")
            if ttl_seconds is not None:
                if ttl_seconds <= 0:
                    con.rollback()
                    raise ValueError("lease ttl_seconds must be positive")
                current["ttl_seconds"] = float(ttl_seconds)
            current["heartbeat_at"] = _now()
            con.execute("UPDATE kv SET payload=? WHERE key='lease'", (json.dumps(current, sort_keys=True),))
            con.commit()
            return current

    def lease_status(self) -> dict[str, Any] | None:
        payload = self._get("lease")
        if not payload:
            return None
        current = json.loads(payload)
        return {**current, "live": self._lease_is_live(current)}

    def release_lease(self, token: str | None = None):
        expected = token or self._lease_token
        if not expected:
            return
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM kv WHERE key='lease'").fetchone()
            if row and json.loads(row[0]).get("token") == expected:
                con.execute("DELETE FROM kv WHERE key='lease'")
            con.commit()
        if self._lease_token == expected:
            self._lease_token = None

    def break_lease(self, *, actor: str = "human") -> dict[str, Any] | None:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM kv WHERE key='lease'").fetchone()
            if not row:
                con.rollback()
                return None
            current = json.loads(row[0])
            con.execute("DELETE FROM kv WHERE key='lease'")
            con.commit()
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


def migrate_run_state(store: RunStoreProtocol, *, actor: str = "operator") -> dict[str, Any]:
    source = store.source_state_version()
    if source is None:
        raise FileNotFoundError("run state does not exist")
    if source == RUN_STATE_CURRENT:
        return {"changed": False, "from": source, "to": RUN_STATE_CURRENT, "backup": None}
    ok, error = store.verify_state_integrity()
    if not ok:
        raise ValueError(f"refusing to migrate invalid state: {error}")
    backup = store.backup_state_snapshot()
    state = store.load_state()
    store.save_state(state)
    store.append_system_event(
        state, "state.migrated", from_version=source, to_version=RUN_STATE_CURRENT, actor=actor, backup=backup
    )
    return {"changed": True, "from": source, "to": RUN_STATE_CURRENT, "backup": backup}


def open_run_store(run_dir: str | Path, backend: str = "auto") -> RunStoreProtocol:
    root = Path(run_dir)
    if backend == "auto":
        backend = "sqlite" if (root / "run.sqlite3").exists() else "filesystem"
    if backend == "filesystem":
        return RunStore(root)
    if backend == "sqlite":
        return SQLiteRunStore(root)
    raise ValueError(f"unknown store backend: {backend}")
