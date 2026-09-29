from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterable, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .cancellation import MemoryCancellationToken
from .integrity import canonical_hash
from .models import CompiledPlan, RunStatus
from .negotiation import requirements_for_plan
from .storage import open_run_store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


class DistributedError(RuntimeError):
    pass


class QueueConflict(DistributedError):
    pass


class ClaimLost(DistributedError):
    pass


class TenantScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "agent-playbook-os/tenant-scope/v1"
    tenant_id: str
    namespace: str = "default"

    @field_validator("tenant_id", "namespace")
    @classmethod
    def _safe_segment(cls, value: str) -> str:
        if not value or len(value) > 64:
            raise ValueError("tenant/namespace must be 1..64 characters")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        if value in {".", ".."} or any(ch not in allowed for ch in value) or "/" in value or "\\" in value:
            raise ValueError("tenant/namespace must be a safe single path segment")
        return value


class WorkStatus(str):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEAD = "DEAD"
    CANCELLED = "CANCELLED"


class WorkSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "agent-playbook-os/work-submission/v1"
    work_id: str = Field(default_factory=lambda: str(uuid4()))
    queue_name: str = "default"
    tenant_id: str = "default"
    namespace: str = "default"
    run_id: str
    plan_path: str
    run_dir: str
    store_backend: str = "filesystem"
    required_capabilities: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    approval_actor: str = "worker"
    max_attempts: int = Field(default=3, ge=1, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("queue_name", "tenant_id", "namespace")
    @classmethod
    def _safe_segment(cls, value: str) -> str:
        return TenantScope._safe_segment(value)

    def semantic_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data.pop("work_id", None)
        return data

    @property
    def payload_hash(self) -> str:
        return canonical_hash(self.semantic_payload())


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "agent-playbook-os/work-item/v1"
    work_id: str
    queue_name: str
    tenant_id: str
    namespace: str
    run_id: str
    plan_path: str
    run_dir: str
    store_backend: str
    required_capabilities: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    approval_actor: str = "worker"
    payload_hash: str
    status: str = WorkStatus.PENDING
    attempts: int = 0
    max_attempts: int = 3
    available_at: float = Field(default_factory=_now_ts)
    claim_token: str | None = None
    fence: int = 0
    claimed_by: str | None = None
    claimed_at: float | None = None
    heartbeat_at: float | None = None
    visibility_timeout_seconds: float | None = None
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class QueueStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "agent-playbook-os/queue-stats/v1"
    queue_name: str
    total: int
    by_status: dict[str, int] = Field(default_factory=dict)


@runtime_checkable
class WorkQueueProtocol(Protocol):
    def enqueue(self, submission: WorkSubmission) -> WorkItem: ...
    def get(self, work_id: str) -> WorkItem | None: ...
    def claim(
        self,
        *,
        worker_id: str,
        capabilities: set[str],
        queue_name: str = "default",
        tenant_id: str | None = None,
        namespace: str | None = None,
        visibility_timeout_seconds: float = 30.0,
    ) -> WorkItem | None: ...
    def heartbeat(self, work_id: str, claim_token: str, fence: int) -> WorkItem: ...
    def ack(self, work_id: str, claim_token: str, fence: int, result: dict[str, Any] | None = None) -> WorkItem: ...
    def nack(self, work_id: str, claim_token: str, fence: int, error: str, *, retry_delay_seconds: float = 0.0) -> WorkItem: ...
    def request_cancel(self, work_id: str) -> WorkItem: ...
    def list(
        self,
        *,
        queue_name: str | None = None,
        tenant_id: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[WorkItem]: ...
    def stats(self, queue_name: str = "default") -> QueueStats: ...


def _requirements_satisfied(required: Iterable[str], available: set[str]) -> bool:
    return set(required).issubset(available)


def required_capabilities_for_plan(plan: CompiledPlan) -> list[str]:
    return sorted({req.id for req in requirements_for_plan(plan) if req.mandatory})


class SQLiteWorkQueue:
    """Transactional reference work queue with claim fencing and idempotent submission.

    SQLite is intentionally the reference backend rather than the distributed production
    recommendation. Its value is executable queue semantics: idempotent submit, bounded
    retries, visibility timeout, stale-claim reclamation and monotonically increasing
    fencing numbers. External adapters should pass the distributed conformance suite.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS work_items (
                    work_id TEXT PRIMARY KEY,
                    queue_name TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    plan_path TEXT NOT NULL,
                    run_dir TEXT NOT NULL,
                    store_backend TEXT NOT NULL,
                    required_capabilities TEXT NOT NULL,
                    approvals TEXT NOT NULL,
                    approval_actor TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    available_at REAL NOT NULL,
                    claim_token TEXT,
                    fence INTEGER NOT NULL,
                    claimed_by TEXT,
                    claimed_at REAL,
                    heartbeat_at REAL,
                    visibility_timeout_seconds REAL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    error TEXT,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_work_claim ON work_items(queue_name,status,available_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_work_tenant ON work_items(tenant_id,namespace,status)")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30.0)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> WorkItem:
        data = dict(row)
        for key in ("required_capabilities", "approvals", "metadata"):
            data[key] = json.loads(data[key])
        data["result"] = json.loads(data["result"]) if data.get("result") else None
        data["cancel_requested"] = bool(data["cancel_requested"])
        return WorkItem.model_validate(data)

    def enqueue(self, submission: WorkSubmission) -> WorkItem:
        now = _now_iso()
        payload = submission.payload_hash
        item = WorkItem(
            **submission.model_dump(mode="json"),
            payload_hash=payload,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    """
                    INSERT INTO work_items(
                        work_id,queue_name,tenant_id,namespace,run_id,plan_path,run_dir,store_backend,
                        required_capabilities,approvals,approval_actor,payload_hash,status,attempts,max_attempts,
                        available_at,claim_token,fence,claimed_by,claimed_at,heartbeat_at,visibility_timeout_seconds,
                        cancel_requested,result,error,metadata,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item.work_id,item.queue_name,item.tenant_id,item.namespace,item.run_id,item.plan_path,item.run_dir,
                        item.store_backend,json.dumps(item.required_capabilities),json.dumps(item.approvals),item.approval_actor,
                        item.payload_hash,item.status,item.attempts,item.max_attempts,item.available_at,None,item.fence,None,
                        None,None,None,0,None,None,json.dumps(item.metadata, sort_keys=True),item.created_at,item.updated_at,
                    ),
                )
                con.commit()
                return item
            except sqlite3.IntegrityError:
                con.rollback()
        existing = self.get(submission.work_id)
        if existing is None:
            raise QueueConflict(f"work_id conflict could not be resolved: {submission.work_id}")
        if existing.payload_hash != payload:
            raise QueueConflict(f"work_id reused with different payload: {submission.work_id}")
        return existing

    def get(self, work_id: str) -> WorkItem | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            return self._row_to_item(row) if row else None

    def _reclaim_expired(self, con: sqlite3.Connection, now: float) -> None:
        rows = con.execute(
            "SELECT work_id, attempts, max_attempts, heartbeat_at, claimed_at, visibility_timeout_seconds "
            "FROM work_items WHERE status=?",
            (WorkStatus.CLAIMED,),
        ).fetchall()
        for row in rows:
            heartbeat = row["heartbeat_at"] or row["claimed_at"] or 0.0
            timeout = row["visibility_timeout_seconds"] or 30.0
            if heartbeat + timeout > now:
                continue
            status = WorkStatus.DEAD if row["attempts"] >= row["max_attempts"] else WorkStatus.PENDING
            con.execute(
                "UPDATE work_items SET status=?, claim_token=NULL, claimed_by=NULL, claimed_at=NULL, heartbeat_at=NULL, "
                "visibility_timeout_seconds=NULL, available_at=?, error=?, updated_at=? WHERE work_id=?",
                (status, now, "claim visibility timeout expired", _now_iso(), row["work_id"]),
            )

    def claim(
        self,
        *,
        worker_id: str,
        capabilities: set[str],
        queue_name: str = "default",
        tenant_id: str | None = None,
        namespace: str | None = None,
        visibility_timeout_seconds: float = 30.0,
    ) -> WorkItem | None:
        if visibility_timeout_seconds <= 0:
            raise ValueError("visibility_timeout_seconds must be positive")
        now = _now_ts()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._reclaim_expired(con, now)
            where = ["queue_name=?", "status=?", "available_at<=?", "cancel_requested=0"]
            params: list[Any] = [queue_name, WorkStatus.PENDING, now]
            if tenant_id is not None:
                where.append("tenant_id=?")
                params.append(tenant_id)
            if namespace is not None:
                where.append("namespace=?")
                params.append(namespace)
            rows = con.execute(
                "SELECT * FROM work_items WHERE " + " AND ".join(where) + " ORDER BY created_at, work_id LIMIT 256",
                tuple(params),
            ).fetchall()
            chosen = None
            for row in rows:
                required = set(json.loads(row["required_capabilities"]))
                if _requirements_satisfied(required, capabilities):
                    chosen = row
                    break
            if chosen is None:
                con.commit()
                return None
            claim_token = str(uuid4())
            fence = int(chosen["fence"]) + 1
            attempts = int(chosen["attempts"]) + 1
            con.execute(
                "UPDATE work_items SET status=?,attempts=?,claim_token=?,fence=?,claimed_by=?,claimed_at=?,heartbeat_at=?,"
                "visibility_timeout_seconds=?,updated_at=? WHERE work_id=? AND status=?",
                (
                    WorkStatus.CLAIMED,attempts,claim_token,fence,worker_id,now,now,float(visibility_timeout_seconds),
                    _now_iso(),chosen["work_id"],WorkStatus.PENDING,
                ),
            )
            if con.total_changes < 1:
                con.rollback()
                return None
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (chosen["work_id"],)).fetchone()
            con.commit()
            return self._row_to_item(row)

    def _assert_claim(self, con: sqlite3.Connection, work_id: str, claim_token: str, fence: int) -> sqlite3.Row:
        row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if row is None:
            raise ClaimLost(f"work item not found: {work_id}")
        if row["status"] != WorkStatus.CLAIMED or row["claim_token"] != claim_token or int(row["fence"]) != int(fence):
            raise ClaimLost(f"claim lost or fenced out for work_id={work_id}")
        return row

    def heartbeat(self, work_id: str, claim_token: str, fence: int) -> WorkItem:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._assert_claim(con, work_id, claim_token, fence)
            if bool(row["cancel_requested"]):
                con.rollback()
                raise ClaimLost(f"work item cancellation requested: {work_id}")
            con.execute(
                "UPDATE work_items SET heartbeat_at=?,updated_at=? WHERE work_id=?",
                (_now_ts(), _now_iso(), work_id),
            )
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            con.commit()
            return self._row_to_item(row)

    def ack(self, work_id: str, claim_token: str, fence: int, result: dict[str, Any] | None = None) -> WorkItem:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._assert_claim(con, work_id, claim_token, fence)
            con.execute(
                "UPDATE work_items SET status=?,result=?,error=NULL,claim_token=NULL,claimed_by=NULL,claimed_at=NULL,"
                "heartbeat_at=NULL,visibility_timeout_seconds=NULL,updated_at=? WHERE work_id=?",
                (WorkStatus.COMPLETED, json.dumps(result, sort_keys=True) if result is not None else None, _now_iso(), work_id),
            )
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            con.commit()
            return self._row_to_item(row)

    def nack(self, work_id: str, claim_token: str, fence: int, error: str, *, retry_delay_seconds: float = 0.0) -> WorkItem:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._assert_claim(con, work_id, claim_token, fence)
            terminal = int(row["attempts"]) >= int(row["max_attempts"])
            status = WorkStatus.DEAD if terminal else WorkStatus.PENDING
            con.execute(
                "UPDATE work_items SET status=?,available_at=?,error=?,claim_token=NULL,claimed_by=NULL,claimed_at=NULL,"
                "heartbeat_at=NULL,visibility_timeout_seconds=NULL,updated_at=? WHERE work_id=?",
                (status, _now_ts() + retry_delay_seconds, str(error), _now_iso(), work_id),
            )
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            con.commit()
            return self._row_to_item(row)

    def request_cancel(self, work_id: str) -> WorkItem:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            if row is None:
                con.rollback()
                raise KeyError(work_id)
            if row["status"] == WorkStatus.PENDING:
                con.execute(
                    "UPDATE work_items SET status=?,cancel_requested=1,updated_at=? WHERE work_id=?",
                    (WorkStatus.CANCELLED, _now_iso(), work_id),
                )
            elif row["status"] == WorkStatus.CLAIMED:
                con.execute(
                    "UPDATE work_items SET cancel_requested=1,updated_at=? WHERE work_id=?",
                    (_now_iso(), work_id),
                )
            row = con.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
            con.commit()
            return self._row_to_item(row)

    def list(
        self,
        *,
        queue_name: str | None = None,
        tenant_id: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[WorkItem]:
        if limit <= 0 or limit > 10_000:
            raise ValueError("limit must be in 1..10000")
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("queue_name", queue_name), ("tenant_id", tenant_id), ("namespace", namespace), ("status", status)
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        sql = "SELECT * FROM work_items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, work_id LIMIT ?"
        params.append(limit)
        with self._connect() as con:
            return [self._row_to_item(row) for row in con.execute(sql, tuple(params)).fetchall()]

    def stats(self, queue_name: str = "default") -> QueueStats:
        with self._connect() as con:
            rows = con.execute(
                "SELECT status,COUNT(*) AS n FROM work_items WHERE queue_name=? GROUP BY status", (queue_name,)
            ).fetchall()
        by_status = {str(row["status"]): int(row["n"]) for row in rows}
        return QueueStats(queue_name=queue_name, total=sum(by_status.values()), by_status=by_status)


@dataclass
class WorkerResult:
    work: WorkItem
    state_status: str | None = None
    error: str | None = None


class DistributedWorker:
    """Reference worker that executes compiled plans claimed from a WorkQueueProtocol."""

    def __init__(
        self,
        *,
        queue: WorkQueueProtocol,
        runtime: Any,
        runner_factory: Callable[..., Any],
        queue_name: str = "default",
        worker_id: str | None = None,
        concurrency: int = 1,
        visibility_timeout_seconds: float = 30.0,
        heartbeat_seconds: float | None = None,
        tenant_id: str | None = None,
        namespace: str | None = None,
        retry_delay_seconds: float = 0.0,
        strict_capabilities: bool = True,
        policy_resolver: Callable[[WorkItem], Any] | None = None,
        coordinator: FencingCoordinatorProtocol | None = None,
        fencing_ttl_seconds: float | None = None,
        allowed_run_root: str | Path | None = None,
        allowed_plan_root: str | Path | None = None,
    ):
        if concurrency < 1 or concurrency > 64:
            raise ValueError("concurrency must be in 1..64")
        if visibility_timeout_seconds <= 0:
            raise ValueError("visibility_timeout_seconds must be positive")
        self.queue = queue
        self.runtime = runtime
        self.runner_factory = runner_factory
        self.queue_name = queue_name
        self.worker_id = worker_id or f"worker-{uuid4()}"
        self.concurrency = concurrency
        self.visibility_timeout_seconds = float(visibility_timeout_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds or max(0.25, visibility_timeout_seconds / 3.0))
        self.tenant_id = tenant_id
        self.namespace = namespace
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.strict_capabilities = strict_capabilities
        self.policy_resolver = policy_resolver
        self.coordinator = coordinator
        self.fencing_ttl_seconds = float(fencing_ttl_seconds or visibility_timeout_seconds)
        if self.fencing_ttl_seconds <= 0:
            raise ValueError("fencing_ttl_seconds must be positive")
        self.allowed_run_root = Path(allowed_run_root).resolve() if allowed_run_root is not None else None
        self.allowed_plan_root = Path(allowed_plan_root).resolve() if allowed_plan_root is not None else None

    def capabilities(self) -> set[str]:
        fn = getattr(self.runtime, "capabilities", None)
        if not callable(fn):
            return set()
        registry = fn()
        ids = {x.id for x in registry.list()}
        ids.update(f"kind:{x.kind}" for x in registry.list())
        return ids

    async def _heartbeat_claim(self, item: WorkItem, cancellation: MemoryCancellationToken, lease: FencingLease | None = None):
        current = lease
        try:
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                self.queue.heartbeat(item.work_id, item.claim_token or "", item.fence)
                if self.coordinator is not None and current is not None:
                    current = self.coordinator.renew(current)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cancellation.cancel(f"distributed claim/fence lost: {exc}")

    async def _execute_claim(self, item: WorkItem) -> WorkerResult:
        cancellation = MemoryCancellationToken()
        lease = None
        resource = f"{item.tenant_id}/{item.namespace}/run/{item.run_id}"
        if self.coordinator is not None:
            try:
                lease = self.coordinator.acquire(resource, self.worker_id, ttl_seconds=self.fencing_ttl_seconds)
            except Exception as exc:
                try:
                    final = self.queue.nack(item.work_id, item.claim_token or "", item.fence, f"fencing lease unavailable: {exc}", retry_delay_seconds=self.retry_delay_seconds)
                except Exception:
                    final = item
                return WorkerResult(final, error=str(exc))
        heartbeat = asyncio.create_task(self._heartbeat_claim(item, cancellation, lease))
        try:
            plan_path = Path(item.plan_path).resolve()
            run_dir = Path(item.run_dir).resolve()
            if self.allowed_plan_root is not None and not plan_path.is_relative_to(self.allowed_plan_root):
                raise DistributedError("work item plan_path escapes allowed_plan_root")
            if self.allowed_run_root is not None and not run_dir.is_relative_to(self.allowed_run_root):
                raise DistributedError("work item run_dir escapes allowed_run_root")
            plan = CompiledPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
            if self.strict_capabilities:
                missing = sorted(set(item.required_capabilities) - self.capabilities())
                if missing:
                    raise DistributedError("worker capability mismatch after claim: " + ", ".join(missing))
            if self.policy_resolver is not None:
                from .policy import assert_plan_satisfies_host_policy
                assert_plan_satisfies_host_policy(plan, self.policy_resolver(item))
            store = open_run_store(item.run_dir, item.store_backend)
            if store.has_state():
                existing = store.load_state()
                if existing.status == RunStatus.COMPLETED:
                    result = {"run_id": existing.run_id, "status": existing.status.value, "recovered": True}
                    if self.coordinator is not None and lease is not None:
                        self.coordinator.assert_current(lease)
                    final = self.queue.ack(item.work_id, item.claim_token or "", item.fence, result)
                    return WorkerResult(final, existing.status.value)
                resume = True
            else:
                resume = False
            runner = self.runner_factory(store_backend=item.store_backend, item=item)
            state = await runner.run(
                plan,
                item.run_dir,
                approvals=set(item.approvals),
                approval_actor=item.approval_actor,
                resume=resume,
                cancellation_token=cancellation,
                lease_owner=f"{self.worker_id}:work={item.work_id}:fence={item.fence}",
                run_metadata={
                    **item.metadata,
                    "distributed": {
                        "work_id": item.work_id,
                        "worker_id": self.worker_id,
                        "queue_name": item.queue_name,
                        "tenant_id": item.tenant_id,
                        "namespace": item.namespace,
                        "fence": item.fence,
                    },
                },
            )
            result = {"run_id": state.run_id, "status": state.status.value, "outputs": state.outputs}
            if self.coordinator is not None and lease is not None:
                self.coordinator.assert_current(lease)
            final = self.queue.ack(item.work_id, item.claim_token or "", item.fence, result)
            return WorkerResult(final, state.status.value)
        except Exception as exc:
            try:
                final = self.queue.nack(
                    item.work_id, item.claim_token or "", item.fence, str(exc), retry_delay_seconds=self.retry_delay_seconds
                )
            except Exception as claim_exc:
                return WorkerResult(item, error=f"{exc}; additionally lost claim during nack: {claim_exc}")
            return WorkerResult(final, error=str(exc))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except BaseException:
                pass
            if self.coordinator is not None and lease is not None:
                try:
                    self.coordinator.release(lease)
                except ClaimLost:
                    pass

    async def run_once(self) -> list[WorkerResult]:
        claimed: list[WorkItem] = []
        caps = self.capabilities()
        for _ in range(self.concurrency):
            item = self.queue.claim(
                worker_id=self.worker_id,
                capabilities=caps,
                queue_name=self.queue_name,
                tenant_id=self.tenant_id,
                namespace=self.namespace,
                visibility_timeout_seconds=self.visibility_timeout_seconds,
            )
            if item is None:
                break
            claimed.append(item)
        if not claimed:
            return []
        return list(await asyncio.gather(*(self._execute_claim(item) for item in claimed)))

    async def run_forever(self, *, poll_seconds: float = 1.0, stop_event: asyncio.Event | None = None):
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        while stop_event is None or not stop_event.is_set():
            results = await self.run_once()
            if not results:
                try:
                    if stop_event is None:
                        await asyncio.sleep(poll_seconds)
                    else:
                        await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass


def distributed_queue_conformance(queue_factory: Callable[[], WorkQueueProtocol]) -> dict[str, Any]:
    """Run semantic probes expected from queue adapters.

    Each probe uses its own queue name to avoid cross-probe claim leakage. The report
    intentionally checks semantics, not just method presence.
    """
    checks: list[dict[str, Any]] = []

    def record(name: str, fn):
        try:
            fn()
            checks.append({"name": name, "passed": True})
        except Exception as exc:
            checks.append({"name": name, "passed": False, "error": str(exc)})

    def submit(q, queue_name, work_id=None, required=None):
        return WorkSubmission(
            work_id=work_id or str(uuid4()), queue_name=queue_name, tenant_id="t1", namespace="n1",
            run_id=str(uuid4()), plan_path="/tmp/plan.json", run_dir="/tmp/run", required_capabilities=required or [],
        )

    def idempotency_probe():
        q = queue_factory(); s = submit(q, "probe-idempotency", "stable")
        a = q.enqueue(s); b = q.enqueue(s)
        assert a.work_id == b.work_id
        changed = s.model_copy(update={"run_id": "different"})
        try:
            q.enqueue(changed)
        except QueueConflict:
            return
        raise AssertionError("queue accepted conflicting payload for existing work_id")

    def capability_probe():
        q = queue_factory(); q.enqueue(submit(q, "probe-cap", required=["action:set"]))
        assert q.claim(worker_id="w1", capabilities=set(), queue_name="probe-cap") is None
        item = q.claim(worker_id="w2", capabilities={"action:set"}, queue_name="probe-cap")
        assert item is not None
        q.ack(item.work_id, item.claim_token or "", item.fence, {"ok": True})

    def fencing_probe():
        q = queue_factory(); q.enqueue(submit(q, "probe-fence"))
        first = q.claim(worker_id="w1", capabilities=set(), queue_name="probe-fence", visibility_timeout_seconds=0.01)
        assert first is not None
        time.sleep(0.02)
        second = q.claim(worker_id="w2", capabilities=set(), queue_name="probe-fence", visibility_timeout_seconds=1.0)
        assert second is not None and second.fence > first.fence
        try:
            q.ack(first.work_id, first.claim_token or "", first.fence, {})
        except ClaimLost:
            pass
        else:
            raise AssertionError("stale claim was able to ack after fencing")
        q.ack(second.work_id, second.claim_token or "", second.fence, {})

    def retry_probe():
        q = queue_factory(); q.enqueue(submit(q, "probe-retry").model_copy(update={"max_attempts": 2}))
        a = q.claim(worker_id="w", capabilities=set(), queue_name="probe-retry")
        assert a is not None
        q.nack(a.work_id, a.claim_token or "", a.fence, "fail")
        b = q.claim(worker_id="w", capabilities=set(), queue_name="probe-retry")
        assert b is not None and b.attempts == 2
        dead = q.nack(b.work_id, b.claim_token or "", b.fence, "fail-again")
        assert dead.status == WorkStatus.DEAD

    record("idempotent-submit", idempotency_probe)
    record("capability-routing", capability_probe)
    record("claim-fencing", fencing_probe)
    record("bounded-retry", retry_probe)

    passed = all(x["passed"] for x in checks)
    return {"passed": passed, "checks": checks}


class FencingLease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "agent-playbook-os/fencing-lease/v1"
    resource: str
    owner: str
    token: str
    fence: int
    acquired_at: float
    heartbeat_at: float
    ttl_seconds: float


@runtime_checkable
class FencingCoordinatorProtocol(Protocol):
    def acquire(self, resource: str, owner: str, *, ttl_seconds: float = 30.0) -> FencingLease: ...
    def renew(self, lease: FencingLease) -> FencingLease: ...
    def assert_current(self, lease: FencingLease) -> None: ...
    def release(self, lease: FencingLease) -> None: ...


class SQLiteFencingCoordinator:
    """Monotonic fencing coordinator for the local reference distributed profile.

    A new acquisition always increments the resource fence. Stale holders cannot renew,
    release, or assert ownership after another worker acquires a newer fence.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS fences ("
                "resource TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL, fence INTEGER NOT NULL, "
                "acquired_at REAL NOT NULL, heartbeat_at REAL NOT NULL, ttl_seconds REAL NOT NULL)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS fence_counters (resource TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30.0)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _expired(row: sqlite3.Row, now: float) -> bool:
        return float(row["heartbeat_at"]) + float(row["ttl_seconds"]) <= now

    @staticmethod
    def _row(row: sqlite3.Row) -> FencingLease:
        return FencingLease(**dict(row))

    def acquire(self, resource: str, owner: str, *, ttl_seconds: float = 30.0) -> FencingLease:
        if not resource or ttl_seconds <= 0:
            raise ValueError("resource must be non-empty and ttl_seconds positive")
        now = _now_ts()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT * FROM fences WHERE resource=?", (resource,)).fetchone()
            if current is not None and not self._expired(current, now):
                con.rollback()
                raise ClaimLost(f"resource already leased: {resource} by {current['owner']}")
            counter = con.execute("SELECT value FROM fence_counters WHERE resource=?", (resource,)).fetchone()
            fence = (int(counter[0]) if counter else 0) + 1
            con.execute(
                "INSERT INTO fence_counters(resource,value) VALUES(?,?) "
                "ON CONFLICT(resource) DO UPDATE SET value=excluded.value",
                (resource, fence),
            )
            token = str(uuid4())
            con.execute(
                "INSERT INTO fences(resource,owner,token,fence,acquired_at,heartbeat_at,ttl_seconds) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(resource) DO UPDATE SET owner=excluded.owner,token=excluded.token,fence=excluded.fence,"
                "acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,ttl_seconds=excluded.ttl_seconds",
                (resource, owner, token, fence, now, now, float(ttl_seconds)),
            )
            row = con.execute("SELECT * FROM fences WHERE resource=?", (resource,)).fetchone()
            con.commit()
            return self._row(row)

    def assert_current(self, lease: FencingLease) -> None:
        now = _now_ts()
        with self._connect() as con:
            row = con.execute("SELECT * FROM fences WHERE resource=?", (lease.resource,)).fetchone()
        if row is None or row["token"] != lease.token or int(row["fence"]) != lease.fence:
            raise ClaimLost(f"fenced lease is stale: {lease.resource}")
        if self._expired(row, now):
            raise ClaimLost(f"fenced lease expired: {lease.resource}")

    def renew(self, lease: FencingLease) -> FencingLease:
        now = _now_ts()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM fences WHERE resource=?", (lease.resource,)).fetchone()
            if row is None or row["token"] != lease.token or int(row["fence"]) != lease.fence:
                con.rollback()
                raise ClaimLost(f"fenced lease is stale: {lease.resource}")
            if self._expired(row, now):
                con.rollback()
                raise ClaimLost(f"fenced lease expired before renewal: {lease.resource}")
            con.execute("UPDATE fences SET heartbeat_at=? WHERE resource=?", (now, lease.resource))
            row = con.execute("SELECT * FROM fences WHERE resource=?", (lease.resource,)).fetchone()
            con.commit()
            return self._row(row)

    def release(self, lease: FencingLease) -> None:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM fences WHERE resource=?", (lease.resource,)).fetchone()
            if row is None:
                con.commit()
                return
            if row["token"] != lease.token or int(row["fence"]) != lease.fence:
                con.rollback()
                raise ClaimLost(f"stale holder cannot release newer fence: {lease.resource}")
            con.execute("DELETE FROM fences WHERE resource=?", (lease.resource,))
            con.commit()
