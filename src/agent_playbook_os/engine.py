from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import json
import time
from typing import Any

import jsonschema

from .budgets import BudgetController
from .compiler import Compiler
from .errors import (
    ApprovalRequired, BudgetExceeded, CancellationRequested, CapabilityNegotiationError, IntegrityError, LeaseConflict, ReconciliationRequired, SideEffectOutcomeUnknown, StepExecutionError
)
from .expressions import render, safe_eval
from .integrity import compiled_plan_integrity
from .loader import load_playbook
from .models import (
    Event,
    RunState,
    RunStatus,
    RuntimeResult,
    RuntimeStreamEvent,
    ApprovalRecord,
    InvocationRecord,
    InvocationStatus,
    StepSpec,
    StepState,
    StepStatus,
    UsageMetrics,
)
from .policy import PolicyEngine
from .runtime import ExecutionRuntime
from .store import RunStore
from .secret_resolver import resolve_secret_references, redact_secret_values
from .cancellation import StoreCancellationToken
from .telemetry import NullTelemetrySink, redact
from .integrity import canonical_hash
from .negotiation import negotiate_runtime


def now():
    return datetime.now(timezone.utc).isoformat()


class Runner:
    def __init__(
        self,
        runtime: ExecutionRuntime,
        compiler: Compiler | None = None,
        telemetry=None,
        schema_resolver=None,
        *,
        secret_resolver=None,
        store_factory=RunStore,
        lease_ttl_seconds: float = 30.0,
        lease_heartbeat_seconds: float | None = None,
        enforce_capabilities: bool = False,
    ):
        self.runtime = runtime
        self.compiler = compiler or Compiler()
        self.telemetry = telemetry or NullTelemetrySink()
        self.schema_resolver = schema_resolver
        self.secret_resolver = secret_resolver
        self.store_factory = store_factory
        self.lease_ttl_seconds = float(lease_ttl_seconds)
        self.lease_heartbeat_seconds = (
            float(lease_heartbeat_seconds)
            if lease_heartbeat_seconds is not None
            else max(0.25, self.lease_ttl_seconds / 3.0)
        )
        if self.lease_ttl_seconds <= 0 or self.lease_heartbeat_seconds <= 0:
            raise ValueError("lease TTL and heartbeat interval must be positive")
        self.enforce_capabilities = enforce_capabilities
        self._seq = 0
        self._active_approvals: set[str] = set()
        self._approval_actor = "human"
        self._cancellation = None
        self._lease_error: Exception | None = None
        self._lease_failure_event = None

    def _emit(self, store, state, event_type, step_id=None, **data):
        self._seq += 1
        safe_data = redact(data)
        event = Event(seq=self._seq, run_id=state.run_id, type=event_type, step_id=step_id, data=safe_data)
        store.append_event(event)
        try:
            self.telemetry.emit(event.model_dump(mode="json"))
        except Exception:
            # Observability extensions must never change workflow success.
            pass

    def _check_cancelled(self):
        if self._lease_error is not None:
            raise LeaseConflict(f"run lease heartbeat failed: {self._lease_error}")
        if self._cancellation is not None and self._cancellation.is_cancelled():
            raise CancellationRequested(self._cancellation.reason() or "cancellation requested")

    def _runtime_capabilities_hash(self) -> str | None:
        fn = getattr(self.runtime, "capabilities", None)
        if not callable(fn):
            return None
        try:
            registry = fn()
            items = [x.model_dump(mode="json") for x in registry.list()]
        except Exception:
            return None
        return canonical_hash(items)

    async def _heartbeat_lease(self, store, token: str):
        try:
            while True:
                await asyncio.sleep(self.lease_heartbeat_seconds)
                store.renew_lease(token, ttl_seconds=self.lease_ttl_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._lease_error = exc
            if self._lease_failure_event is not None:
                self._lease_failure_event.set()

    async def _await_with_controls(self, coro, timeout: float | None):
        self._check_cancelled()
        task = asyncio.create_task(coro)
        cancel_task = None
        lease_task = None
        try:
            waiters = {task}
            if self._cancellation is not None:
                cancel_task = asyncio.create_task(self._cancellation.wait())
                waiters.add(cancel_task)
            if self._lease_failure_event is not None:
                lease_task = asyncio.create_task(self._lease_failure_event.wait())
                waiters.add(lease_task)
            if len(waiters) > 1:
                done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if lease_task is not None and lease_task in done:
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass
                    raise LeaseConflict(f"run lease heartbeat failed: {self._lease_error}")
                if cancel_task is not None and cancel_task in done:
                    task.cancel()
                    try:
                        await task
                    except BaseException:
                        pass
                    raise CancellationRequested(self._cancellation.reason() or "cancellation requested")
                if task in done:
                    return await task
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                raise asyncio.TimeoutError()
            return await asyncio.wait_for(task, timeout=timeout) if timeout is not None else await task
        finally:
            # Cancellation of the Runner task itself must not detach its active
            # invocation while releasing the run lease.
            for waiter in (task, cancel_task, lease_task):
                if waiter is not None and not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*(waiter for waiter in (task, cancel_task, lease_task) if waiter is not None), return_exceptions=True)

    def _runtime_supports_isolation(self, mode: str, step: StepSpec) -> bool:
        fn = getattr(self.runtime, "supports_isolation", None)
        return bool(fn(mode, step)) if fn else mode in {"inline", "auto"}

    def _runtime_supports_idempotency(self, step: StepSpec) -> bool:
        fn = getattr(self.runtime, "supports_idempotency", None)
        return bool(fn(step)) if fn else False

    def _effective_isolation(self, step: StepSpec, plan) -> str:
        requested = step.execution_isolation or plan.execution.isolation
        required = plan.policy.require_isolation_for.get(step.side_effects)
        if required:
            if not self._runtime_supports_isolation(required, step):
                raise StepExecutionError(
                    f"step {step.id} requires {required} isolation for {step.side_effects} side effects, "
                    "but runtime cannot prove that boundary"
                )
            return required
        if requested in {"sandbox", "subagent"} and not self._runtime_supports_isolation(requested, step):
            raise StepExecutionError(f"runtime does not support requested isolation={requested} for step {step.id}")
        if requested == "auto":
            return "inline"
        return requested

    def _begin_invocation(self, step: StepSpec, state: RunState, step_state: StepState, store: RunStore, *, step_id: str, attempt: int):
        if step.type not in {"skill", "action", "agent", "eval"}:
            return None
        record = InvocationRecord(
            step_id=step_id,
            attempt=attempt,
            kind=step.type,
            side_effects=step.side_effects,
            idempotency_key=step_state.idempotency_key,
        )
        state.invocations.append(record)
        step_state.invocation_ids.append(record.invocation_id)
        store.save_state(state)
        self._emit(
            store,
            state,
            "invocation.started",
            step_id,
            invocation_id=record.invocation_id,
            attempt=attempt,
            kind=record.kind,
            side_effects=record.side_effects,
            idempotency_key=record.idempotency_key,
        )
        return record

    def _finish_invocation(self, record, state: RunState, store: RunStore, *, output=None, error: BaseException | None = None, cancelled=False, uncertain=False):
        if record is None:
            return
        record.finished_at = now()
        if uncertain or isinstance(error, SideEffectOutcomeUnknown):
            record.status = InvocationStatus.UNKNOWN
            record.error = redact(str(error)) if error is not None else "outcome unknown"
        elif cancelled:
            record.status = InvocationStatus.CANCELLED
        elif error is not None:
            record.status = InvocationStatus.FAILED
            record.error = redact(str(error))
        else:
            record.status = InvocationStatus.SUCCEEDED
            record.result_hash = canonical_hash(redact(output))
        store.save_state(state)
        self._emit(
            store,
            state,
            "invocation.finished",
            record.step_id,
            invocation_id=record.invocation_id,
            status=record.status.value,
            result_hash=record.result_hash,
            error=record.error,
        )

    def _step_for_invocation(self, plan, step_id: str):
        top = {x.spec.id: x.spec for x in plan.steps}
        direct = top.get(step_id)
        if direct is not None:
            return direct
        parts = step_id.split("/", 2)
        if len(parts) == 3:
            parent_id, scope, nested_id = parts
            parent = top.get(parent_id)
            if parent is not None and parent.type == "parallel":
                for nested in parent.branches.get(scope, []):
                    if nested.id == nested_id:
                        return nested
            if parent is not None and parent.type == "foreach" and scope.startswith("item-"):
                for nested in parent.foreach_steps:
                    if nested.id == nested_id:
                        return nested
            if parent is not None and parent.type == "while" and scope.startswith("iteration-"):
                for nested in parent.loop_steps:
                    if nested.id == nested_id:
                        return nested
        return None

    def _reconcile_uncertain_invocations_for_resume(self, state: RunState, plan, store):
        uncertain = [
            x for x in state.invocations
            if x.status == InvocationStatus.UNKNOWN or (
                x.status == InvocationStatus.STARTED and x.side_effects in {"local", "external", "destructive"}
            )
        ]
        for inv in uncertain:
            step = self._step_for_invocation(plan, inv.step_id)
            if step is not None and inv.idempotency_key and self._runtime_supports_idempotency(step):
                inv.status = InvocationStatus.RECONCILED
                inv.finished_at = now()
                inv.reconciliation_note = "auto: adapter-proven idempotent retry"
                self._emit(
                    store,
                    state,
                    "invocation.auto_reconciled",
                    inv.step_id,
                    invocation_id=inv.invocation_id,
                    reason=inv.reconciliation_note,
                )
                continue
            raise ReconciliationRequired(
                f"uncertain {inv.side_effects} invocation {inv.invocation_id} for {inv.step_id}; "
                "reconcile before resume"
            )
        if uncertain:
            store.save_state(state)

    def _context(self, plan, state, *, extra_steps: dict[str, Any] | None = None, control: dict[str, Any] | None = None, extra_context: dict[str, Any] | None = None):
        steps = {}
        for sid, ss in state.steps.items():
            steps[sid] = {
                "status": ss.status.value,
                "output": ss.output,
                "error": ss.error,
                "attempts": ss.attempts,
            }
        if extra_steps:
            steps.update(extra_steps)
        ctx = {"inputs": plan.input_values, "steps": steps, "run": state.model_dump(mode="json")}
        if extra_context:
            for key, value in extra_context.items():
                if key in {"inputs", "steps", "run", "control"}:
                    raise StepExecutionError(f"extra context cannot override reserved key: {key}")
                ctx[key] = value
        if control:
            ctx["control"] = control
        return ctx

    async def run(
        self,
        plan,
        run_dir: str | Path,
        approvals: set[str] | None = None,
        resume: bool = False,
        replayed_from: str | None = None,
        approval_actor: str = "human",
        cancellation_token=None,
        lease_owner: str | None = None,
        allow_runtime_change: bool = False,
        parent_run_id: str | None = None,
        root_run_id: str | None = None,
        parent_lineage_depth: int | None = None,
        fork_reason: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ):
        if self.enforce_capabilities:
            report = negotiate_runtime(plan, self.runtime)
            if not report.passed:
                raise CapabilityNegotiationError(
                    "runtime capability preflight failed: " + ", ".join(report.missing)
                )
        store = self.store_factory(run_dir)
        self._active_approvals = set(approvals or set())
        self._approval_actor = approval_actor
        self._cancellation = cancellation_token or StoreCancellationToken(store)
        self._lease_error = None
        self._lease_failure_event = asyncio.Event()
        token = store.acquire_lease(
            owner=lease_owner or f"runner:{id(self)}",
            ttl_seconds=self.lease_ttl_seconds,
        )
        heartbeat = asyncio.create_task(self._heartbeat_lease(store, token))
        try:
            return await self._run_locked(
                plan,
                store,
                approvals=self._active_approvals,
                resume=resume,
                replayed_from=replayed_from,
                approval_actor=approval_actor,
                allow_runtime_change=allow_runtime_change,
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                parent_lineage_depth=parent_lineage_depth,
                fork_reason=fork_reason,
                run_metadata=run_metadata,
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except BaseException:
                pass
            store.release_lease(token)

    async def _run_locked(
        self,
        plan,
        store,
        approvals: set[str] | None = None,
        resume: bool = False,
        replayed_from: str | None = None,
        approval_actor: str = "human",
        allow_runtime_change: bool = False,
        parent_run_id: str | None = None,
        root_run_id: str | None = None,
        parent_lineage_depth: int | None = None,
        fork_reason: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ):
        approvals = set(approvals or set())
        actual_integrity = compiled_plan_integrity(plan)
        if actual_integrity != plan.integrity_hash:
            raise IntegrityError("compiled plan integrity check failed before execution")

        if resume:
            if not store.has_state():
                raise StepExecutionError("cannot resume: run state does not exist")
            ok, error = store.verify_event_chain()
            if not ok:
                raise IntegrityError(f"cannot resume tampered event chain: {error}")
            ok, error = store.verify_plan_integrity()
            if not ok:
                raise IntegrityError(f"cannot resume tampered plan: {error}")
            ok, error = store.verify_state_integrity()
            if not ok:
                raise IntegrityError(f"cannot resume tampered state: {error}")
            state = store.load_state()
            current_caps = self._runtime_capabilities_hash()
            if state.runtime_capabilities_hash and current_caps and state.runtime_capabilities_hash != current_caps:
                if not allow_runtime_change:
                    raise StepExecutionError(
                        "runtime capabilities changed since the run started; pass allow_runtime_change=True only after review"
                    )
                self._emit(
                    store, state, "runtime.capabilities_changed",
                    previous=state.runtime_capabilities_hash, current=current_caps,
                )
            elif state.runtime_capabilities_hash is None:
                state.runtime_capabilities_hash = current_caps
            self._reconcile_uncertain_invocations_for_resume(state, plan, store)
            stored_plan = store.load_plan()
            if stored_plan.integrity_hash != plan.integrity_hash:
                raise StepExecutionError("stored run plan differs from requested plan")
            if (
                state.plan_hash != plan.integrity_hash
                or state.plan_semantic_hash != plan.semantic_hash
                or state.playbook_hash != plan.playbook_hash
            ):
                raise IntegrityError("run state hashes do not match compiled plan")
            if state.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                raise StepExecutionError(f"cannot resume terminal run: {state.status.value}")
            existing = store.read_events()
            self._seq = max((e.get("seq", 0) for e in existing), default=0)
            self._emit(store, state, "run.resumed", approvals=sorted(approvals))
        else:
            if store.has_any_run_data():
                raise StepExecutionError("run directory already contains run data; use resume or a new run id")
            state = RunState(
                plan_hash=plan.integrity_hash,
                plan_semantic_hash=plan.semantic_hash,
                playbook_hash=plan.playbook_hash,
                playbook_id=plan.playbook_id,
                playbook_version=plan.playbook_version,
                inputs=plan.input_values,
                steps={x.spec.id: StepState(id=x.spec.id) for x in plan.steps},
                replayed_from=replayed_from,
                parent_run_id=parent_run_id,
                root_run_id=root_run_id or parent_run_id,
                lineage_depth=(parent_lineage_depth + 1) if parent_run_id and parent_lineage_depth is not None else (1 if parent_run_id else 0),
                fork_reason=fork_reason,
                metadata=dict(run_metadata or {}),
                runtime_capabilities_hash=self._runtime_capabilities_hash(),
                storage_backend=getattr(store, "backend_id", "filesystem"),
            )
            if state.root_run_id is None:
                state.root_run_id = state.run_id
            store.save_plan(plan)
            store.save_state(state)
            self._emit(
                store,
                state,
                "run.created",
                playbook_hash=plan.playbook_hash,
                plan_integrity_hash=plan.integrity_hash,
                plan_semantic_hash=plan.semantic_hash,
                replayed_from=replayed_from,
                parent_run_id=parent_run_id,
                root_run_id=state.root_run_id,
                lineage_depth=state.lineage_depth,
                fork_reason=fork_reason,
            )

        state.status = RunStatus.RUNNING
        state.started_at = state.started_at or now()
        store.save_state(state)
        policy = PolicyEngine(plan.policy)
        budget = BudgetController(plan)

        for compiled in plan.steps:
            self._check_cancelled()
            step = compiled.spec
            ss = state.steps[step.id]
            if ss.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
                continue

            try:
                budget.check(state)
                if not self._dependencies_satisfied(step, state):
                    raise StepExecutionError(f"dependencies not satisfied for step {step.id}")
                context = self._context(plan, state)
                if step.when and not bool(safe_eval(step.when, context)):
                    ss.status = StepStatus.SKIPPED
                    ss.finished_at = now()
                    store.save_state(state)
                    self._emit(store, state, "step.skipped", step.id, reason="when=false")
                    continue

                policy.check_step(step)
                if ss.idempotency_key is None and step.idempotency_key:
                    rendered_key = render(step.idempotency_key, context)
                    if not isinstance(rendered_key, (str, int, float, bool)):
                        raise StepExecutionError(f"idempotency_key for {step.id} must render to a scalar")
                    ss.idempotency_key = str(rendered_key)

                requires_approval = policy.approval_required(step) or (step.type == "gate" and step.gate == "human")
                already_approved = any(x.step_id == step.id for x in state.approvals)
                if requires_approval and not already_approved and step.id not in approvals:
                    ss.status = StepStatus.WAITING_APPROVAL
                    ss.approval_required = True
                    if step.id not in state.pending_approvals:
                        state.pending_approvals.append(step.id)
                    state.status = RunStatus.WAITING_APPROVAL
                    store.save_state(state)
                    self._emit(store, state, "gate.approval_required", step.id)
                    return state
                if step.id in state.pending_approvals:
                    state.pending_approvals.remove(step.id)
                if requires_approval and not already_approved:
                    record = ApprovalRecord(step_id=step.id, actor=approval_actor)
                    state.approvals.append(record)
                    store.save_state(state)
                    self._emit(store, state, "gate.approved", step.id, actor=record.actor, approved_at=record.approved_at)
                ss.approval_required = False
                effective_isolation = self._effective_isolation(step, plan)

                ss.status = StepStatus.RUNNING
                ss.started_at = ss.started_at or now()
                started = time.perf_counter()
                store.save_state(state)
                self._emit(store, state, "step.started", step.id, type=step.type, idempotency_key=ss.idempotency_key, isolation=effective_isolation)

                output = await self._execute_with_retry(step, plan, state, store, budget)
                self._validate_output_schema(step, output, plan)
                ss.output = output
                ss.status = StepStatus.COMPLETED
                ss.error = None
                ss.finished_at = now()
                ss.duration_ms = (time.perf_counter() - started) * 1000
                budget.check(state)
                store.save_state(state)
                self._emit(store, state, "step.completed", step.id, duration_ms=ss.duration_ms)
            except ApprovalRequired as exc:
                ss.status = StepStatus.WAITING_APPROVAL
                ss.approval_required = True
                if exc.approval_id not in state.pending_approvals:
                    state.pending_approvals.append(exc.approval_id)
                state.status = RunStatus.WAITING_APPROVAL
                store.save_state(state)
                self._emit(store, state, "gate.approval_required", exc.approval_id, parent_step=step.id)
                return state
            except CancellationRequested as exc:
                ss.status = StepStatus.CANCELLED
                ss.error = f"{type(exc).__name__}: {exc}"
                ss.finished_at = now()
                state.status = RunStatus.CANCELLED
                state.cancellation_reason = str(exc)
                state.cancelled_at = now()
                state.finished_at = state.cancelled_at
                state.usage.wall_time_seconds = budget.elapsed_seconds(state)
                store.save_state(state)
                self._emit(store, state, "run.cancelled", step.id, reason=str(exc))
                return state
            except Exception as exc:
                ss.error = f"{type(exc).__name__}: {exc}"
                ss.finished_at = now()
                if step.on_fail == "skip" and not isinstance(exc, (BudgetExceeded, IntegrityError)):
                    ss.status = StepStatus.SKIPPED
                    self._emit(store, state, "step.skipped", step.id, reason=ss.error)
                    store.save_state(state)
                    continue
                if step.on_fail == "continue" and not isinstance(exc, (BudgetExceeded, IntegrityError)):
                    ss.status = StepStatus.FAILED
                    self._emit(store, state, "step.failed_continued", step.id, error=ss.error)
                    store.save_state(state)
                    continue
                ss.status = StepStatus.FAILED
                state.status = RunStatus.FAILED
                state.finished_at = now()
                state.usage.wall_time_seconds = budget.elapsed_seconds(state)
                store.save_state(state)
                self._emit(store, state, "run.failed", step.id, error=ss.error)
                return state

        context = self._context(plan, state)
        state.outputs = render(plan.outputs, context)
        state.status = RunStatus.COMPLETED
        state.finished_at = now()
        state.usage.wall_time_seconds = budget.elapsed_seconds(state)
        store.save_state(state)
        self._emit(store, state, "run.completed", usage=state.usage.model_dump(mode="json"))
        return state

    def _dependencies_satisfied(self, step, state):
        # FAILED can only remain in a running workflow when its own on_fail=continue.
        okay = {StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.FAILED}
        return all(state.steps[d].status in okay for d in step.needs)

    def _increment_invocation_usage(self, step: StepSpec, state: RunState, step_state: StepState):
        state.usage.total_attempts += 1
        step_state.usage.total_attempts += 1
        field = {
            "skill": "skill_calls",
            "action": "action_calls",
            "agent": "agent_calls",
            "eval": "eval_calls",
        }.get(step.type)
        if field:
            setattr(state.usage, field, getattr(state.usage, field) + 1)
            setattr(step_state.usage, field, getattr(step_state.usage, field) + 1)

    def _should_retry(self, step: StepSpec, exc: Exception) -> bool:
        if isinstance(exc, (BudgetExceeded, IntegrityError, CancellationRequested, LeaseConflict, ReconciliationRequired)):
            return False
        if isinstance(exc, SideEffectOutcomeUnknown):
            return bool(step.idempotency_key and self._runtime_supports_idempotency(step))
        if not step.retry.retry_on:
            return True
        names = {cls.__name__ for cls in type(exc).mro()}
        return bool(names & set(step.retry.retry_on))

    async def _execute_with_retry(self, step, plan, state, store, budget: BudgetController):
        attempts = max(1, step.retry.max_attempts)
        last: Exception | None = None
        ss = state.steps[step.id]
        for i in range(attempts):
            if i and step.type in {"parallel", "foreach", "while"}:
                # A container's own default 'none' does not describe effects
                # of its interrupted children. Reuse the durable recovery gate
                # before a retry can dispatch any descendant again.
                self._reconcile_uncertain_invocations_for_resume(state, plan, store)
            self._check_cancelled()
            budget.check(state)
            budget.check_can_invoke(state, step.type)
            ss.attempts += 1
            self._increment_invocation_usage(step, state, ss)
            store.save_state(state)
            attempt = i + 1
            control = {
                "run_id": state.run_id,
                "step_id": step.id,
                "attempt": attempt,
                "idempotency_key": ss.idempotency_key,
                "isolation": self._effective_isolation(step, plan),
            }
            self._emit(store, state, "step.attempt_started", step.id, attempt=attempt, idempotency_key=ss.idempotency_key)
            invocation = self._begin_invocation(
                step, state, ss, store, step_id=step.id, attempt=attempt
            )
            if invocation is not None:
                control["invocation_id"] = invocation.invocation_id
            try:
                timeout = budget.effective_step_timeout(step.timeout_seconds, state)
                if timeout is not None and timeout <= 0:
                    raise BudgetExceeded("no run time budget remains")
                async def invoke_and_consume():
                    raw = await self._execute_step(step, plan, state, store, control, budget)
                    return await self._consume_runtime_output(raw, state, ss, store, step.id, invocation)

                output = await self._await_with_controls(invoke_and_consume(), timeout)
                output = redact_secret_values(output)
                self._finish_invocation(invocation, state, store, output=output)
                budget.check(state)
                return output
            except asyncio.TimeoutError:
                timeout_error = TimeoutError(f"step attempt timed out after {timeout}s")
                if step.side_effects in {"local", "external", "destructive"}:
                    last = SideEffectOutcomeUnknown(str(timeout_error))
                    self._finish_invocation(invocation, state, store, error=last, uncertain=True)
                else:
                    last = timeout_error
                    self._finish_invocation(invocation, state, store, error=last)
                self._emit(store, state, "step.attempt_failed", step.id, attempt=attempt, error=str(last))
            except LeaseConflict as exc:
                if step.side_effects in {"local", "external", "destructive"}:
                    self._finish_invocation(invocation, state, store, error=exc, uncertain=True)
                else:
                    self._finish_invocation(invocation, state, store, error=exc)
                raise
            except (CancellationRequested, asyncio.CancelledError) as exc:
                self._finish_invocation(
                    invocation,
                    state,
                    store,
                    error=exc,
                    cancelled=step.side_effects == "none",
                    uncertain=step.side_effects in {"local", "external", "destructive"},
                )
                raise
            except Exception as exc:
                last = exc
                self._finish_invocation(invocation, state, store, error=exc)
                self._emit(store, state, "step.attempt_failed", step.id, attempt=attempt, error=str(exc))
            if attempt >= attempts or not self._should_retry(step, last):
                break
            delay = min(
                step.retry.backoff_seconds * (step.retry.backoff_multiplier ** (attempt - 1)),
                step.retry.max_backoff_seconds,
            )
            if delay:
                self._emit(store, state, "step.retry_scheduled", step.id, attempt=attempt + 1, delay_seconds=delay)
                await self._await_with_controls(asyncio.sleep(delay), delay + 1.0)
        assert last is not None
        raise last

    async def _consume_runtime_output(self, raw, state, step_state, store, step_id, invocation=None):
        if hasattr(raw, "__aiter__"):
            aggregate = RuntimeResult()
            saw_result = False
            async for item in raw:
                event = item if isinstance(item, RuntimeStreamEvent) else RuntimeStreamEvent.model_validate(item)
                self._emit(store, state, f"runtime.stream.{event.type}", step_id, metadata=event.metadata)
                if event.type == "usage" and event.usage is not None:
                    aggregate.usage.add(event.usage)
                elif event.type == "artifact" and event.artifact is not None:
                    aggregate.artifacts.append(event.artifact)
                elif event.type == "evidence" and event.evidence_ref is not None:
                    aggregate.evidence_refs.append(event.evidence_ref)
                elif event.type == "receipt" and event.receipt is not None:
                    aggregate.provider_receipts.append(event.receipt)
                elif event.type == "result":
                    aggregate.value = event.value
                    saw_result = True
            if not saw_result:
                raise StepExecutionError(f"streaming runtime for {step_id} ended without a result event")
            raw = aggregate
        return self._consume_runtime_result(raw, state, step_state, store, step_id, invocation)

    def _consume_runtime_result(self, raw, state: RunState, step_state: StepState, store, step_id: str, invocation=None):
        if not isinstance(raw, RuntimeResult):
            return raw
        state.usage.add(raw.usage)
        step_state.usage.add(raw.usage)
        existing_artifacts = {x.id for x in state.artifacts}
        for ref in raw.artifacts:
            if ref.id not in existing_artifacts:
                state.artifacts.append(ref)
                existing_artifacts.add(ref.id)
        existing_evidence = {x.id for x in state.evidence_refs}
        for ref in raw.evidence_refs:
            if ref.id not in existing_evidence:
                state.evidence_refs.append(ref)
                existing_evidence.add(ref.id)
        existing_receipts = {x.receipt_id for x in state.provider_receipts}
        receipt_ids = []
        for receipt in raw.provider_receipts:
            safe_receipt = receipt.model_copy(update={"metadata": redact(receipt.metadata)})
            if safe_receipt.idempotency_key is None:
                safe_receipt.idempotency_key = step_state.idempotency_key
            if safe_receipt.receipt_id not in existing_receipts:
                state.provider_receipts.append(safe_receipt)
                existing_receipts.add(safe_receipt.receipt_id)
            receipt_ids.append(safe_receipt.receipt_id)
            if safe_receipt.receipt_id not in step_state.provider_receipt_ids:
                step_state.provider_receipt_ids.append(safe_receipt.receipt_id)
            if invocation is not None and safe_receipt.receipt_id not in invocation.provider_receipt_ids:
                invocation.provider_receipt_ids.append(safe_receipt.receipt_id)
                invocation.provider_call_id = invocation.provider_call_id or safe_receipt.call_id
                invocation.provider = invocation.provider or safe_receipt.provider
        self._emit(
            store,
            state,
            "runtime.result",
            step_id,
            usage=raw.usage.model_dump(mode="json"),
            artifact_ids=[x.id for x in raw.artifacts],
            evidence_ids=[x.id for x in raw.evidence_refs],
            provider_receipt_ids=receipt_ids,
        )
        store.save_state(state)
        return raw.value

    def _validate_output_schema(self, step, output, plan):
        if not step.output_schema:
            return
        ref = step.output_schema
        if ref.startswith("http://") or ref.startswith("https://"):
            if not step.output_schema_hash:
                raise StepExecutionError("remote output_schema is missing required hash pin")
            if self.schema_resolver is None:
                raise StepExecutionError(
                    "remote output_schema requires a schema-resolver adapter; reference runtime fails closed"
                )
            schema = self.schema_resolver.resolve(ref, step.output_schema_hash)
            jsonschema.validate(output, schema)
            return
        base = (Path(plan.source_path).parent if plan.source_path else Path.cwd()).resolve()
        raw = ref[5:] if ref.startswith("file:") else ref
        schema_path = (base / raw).resolve()
        if not schema_path.is_relative_to(base):
            raise StepExecutionError(f"output schema path escapes playbook directory: {ref}")
        payload = schema_path.read_bytes()
        if step.output_schema_hash:
            actual = "sha256:" + __import__("hashlib").sha256(payload).hexdigest()
            expected = step.output_schema_hash if step.output_schema_hash.startswith("sha256:") else "sha256:" + step.output_schema_hash
            if actual.lower() != expected.lower():
                raise IntegrityError(f"local output schema hash mismatch: expected={expected} actual={actual}")
        schema = json.loads(payload.decode("utf-8"))
        jsonschema.validate(output, schema)

    async def _execute_step(self, step, plan, state, store, control, budget: BudgetController):
        context = self._context(plan, state, control=control)
        params = render(step.with_, context)
        params = resolve_secret_references(params, self.secret_resolver)
        if step.type == "skill":
            self._emit(store, state, "skill.invoked", step.id, skill=step.uses, idempotency_key=control.get("idempotency_key"))
            return await self.runtime.execute_skill(step, params, context)
        if step.type == "action":
            self._emit(
                store,
                state,
                "action.invoked",
                step.id,
                action=step.action,
                side_effects=step.side_effects,
                idempotency_key=control.get("idempotency_key"),
            )
            return await self.runtime.execute_action(step, params, context)
        if step.type == "agent":
            self._emit(store, state, "agent.invoked", step.id, isolation=step.execution_isolation or plan.execution.isolation)
            return await self.runtime.execute_agent(step, params, context)
        if step.type == "eval":
            out = await self.runtime.execute_eval(step, params, context)
            if isinstance(out, RuntimeResult):
                candidate = out.value
            else:
                candidate = out
            if isinstance(candidate, dict) and candidate.get("passed") is False:
                raise StepExecutionError(f"eval failed: {candidate}")
            return out
        if step.type == "gate":
            if step.gate == "human":
                return {"approved": True}
            if step.gate == "budget":
                budget.check(state)
                return {"passed": True, "gate": "budget", "usage": state.usage.model_dump(mode="json")}
            if step.condition is None:
                return {"passed": True, "gate": step.gate}
            passed = bool(safe_eval(step.condition, context))
            if not passed:
                raise StepExecutionError(f"gate condition failed: {step.condition}")
            return {"passed": True, "gate": step.gate}
        if step.type == "branch":
            for case in step.cases:
                if bool(safe_eval(case.when, context)):
                    return {"selected": render(case.value, context)}
            return {"selected": render(step.default, context)}
        if step.type == "parallel":
            return await self._execute_parallel(step, plan, state, store, budget, context)
        if step.type == "foreach":
            return await self._execute_foreach(step, plan, state, store, budget, context)
        if step.type == "while":
            return await self._execute_while(step, plan, state, store, budget, context)
        if step.type == "playbook":
            base = (Path(plan.source_path).parent if plan.source_path else Path.cwd()).resolve()
            child_path = (base / step.playbook).resolve()
            if not child_path.is_relative_to(base):
                raise StepExecutionError(f"subplaybook path escapes playbook directory: {step.playbook}")
            child_pb = load_playbook(child_path)
            child_plan = self.compiler.compile(child_pb, params, source_path=str(child_path))
            child_dir = store.run_dir / "subruns" / step.id
            child_runner = Runner(
                self.runtime, self.compiler, telemetry=self.telemetry, schema_resolver=self.schema_resolver,
                secret_resolver=self.secret_resolver, store_factory=self.store_factory,
                lease_ttl_seconds=self.lease_ttl_seconds, lease_heartbeat_seconds=self.lease_heartbeat_seconds,
                enforce_capabilities=self.enforce_capabilities,
            )
            child_state = await child_runner.run(
                child_plan, child_dir, parent_run_id=state.run_id,
                root_run_id=state.root_run_id or state.run_id,
                fork_reason=f"subplaybook:{step.id}",
                parent_lineage_depth=state.lineage_depth,
            )
            if child_state.status != RunStatus.COMPLETED:
                raise StepExecutionError(f"subplaybook {step.id} ended as {child_state.status.value}")
            state.usage.add(child_state.usage)
            budget.check(state)
            return child_state.outputs
        raise StepExecutionError(f"unsupported step type: {step.type}")


    def _foreach_item_prefix(self, parent_id: str, index: int, item: Any) -> str:
        digest = canonical_hash(redact(item)).split(":", 1)[1][:10]
        return f"{parent_id}/item-{index:04d}-{digest}"

    async def _execute_foreach(self, step, plan, state, store, budget, base_context):
        rendered_items = render(step.items, base_context)
        if not isinstance(rendered_items, list):
            raise StepExecutionError(f"foreach step {step.id} items must resolve to an array")
        if len(rendered_items) > plan.execution.max_foreach_items:
            raise StepExecutionError(
                f"foreach step {step.id} resolved {len(rendered_items)} items; "
                f"execution.max_foreach_items={plan.execution.max_foreach_items}"
            )
        concurrency = min(step.max_concurrency or plan.execution.max_parallel, plan.execution.max_parallel)
        semaphore = asyncio.Semaphore(concurrency)

        async def run_item(index, item):
            async with semaphore:
                return index, await self._execute_foreach_item(
                    parent=step, index=index, item=item, plan=plan, state=state, store=store, budget=budget
                )

        results = await asyncio.gather(
            *(run_item(i, item) for i, item in enumerate(rendered_items)),
            return_exceptions=True,
        )
        pairs = []
        errors = []
        for result in results:
            if isinstance(result, BaseException):
                errors.append(result)
            else:
                pairs.append(result)
        if errors:
            for cls in (ApprovalRequired, CancellationRequested, IntegrityError, ReconciliationRequired, BudgetExceeded):
                for error in errors:
                    if isinstance(error, cls):
                        raise error
            raise errors[0]
        ordered = [value for _, value in sorted(pairs, key=lambda x: x[0])]
        return ordered

    async def _execute_foreach_item(self, *, parent, index, item, plan, state, store, budget):
        prefix = self._foreach_item_prefix(parent.id, index, item)
        local: dict[str, dict[str, Any]] = {}
        outputs: dict[str, Any] = {}
        remaining: dict[str, StepSpec] = {}
        extra_context = {parent.item_var: item, "foreach_index": index}

        for nested in parent.foreach_steps:
            key = f"{prefix}/{nested.id}"
            nested_state = state.nested_steps.get(key)
            if nested_state is None:
                nested_state = StepState(id=key)
                state.nested_steps[key] = nested_state
            if nested_state.status in {StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.FAILED}:
                local[nested.id] = self._local_view(nested_state)
                if nested_state.status == StepStatus.COMPLETED:
                    outputs[nested.id] = nested_state.output
            else:
                remaining[nested.id] = nested
        store.save_state(state)

        while remaining:
            self._check_cancelled()
            ready = []
            for nested in remaining.values():
                internal_needs = [d for d in nested.needs if d in local or d in remaining]
                if all(d in local and local[d]["status"] in {"COMPLETED", "SKIPPED", "FAILED"} for d in internal_needs):
                    ready.append(nested)
            if not ready:
                raise StepExecutionError(f"foreach item {index} made no progress; dependency validation drift")

            nested = ready[0]
            del remaining[nested.id]
            key = f"{prefix}/{nested.id}"
            local_state = state.nested_steps[key]
            context = self._context(plan, state, extra_steps=dict(local), extra_context=extra_context)

            if nested.when and not bool(safe_eval(nested.when, context)):
                local_state.status = StepStatus.SKIPPED
                local_state.finished_at = now()
                local[nested.id] = self._local_view(local_state)
                store.save_state(state)
                self._emit(store, state, "foreach.step.skipped", key, parent=parent.id, index=index, reason="when=false")
                continue

            policy = PolicyEngine(plan.policy)
            policy.check_step(nested)
            if local_state.idempotency_key is None and nested.idempotency_key:
                rendered_key = render(nested.idempotency_key, context)
                if not isinstance(rendered_key, (str, int, float, bool)):
                    raise StepExecutionError(f"idempotency_key for {key} must render to a scalar")
                local_state.idempotency_key = str(rendered_key)

            requires_approval = policy.approval_required(nested) or (nested.type == "gate" and nested.gate == "human")
            already_approved = any(x.step_id == key for x in state.approvals)
            if requires_approval and not already_approved and key not in self._active_approvals:
                local_state.status = StepStatus.WAITING_APPROVAL
                local_state.approval_required = True
                if key not in state.pending_approvals:
                    state.pending_approvals.append(key)
                store.save_state(state)
                raise ApprovalRequired(key)
            if key in state.pending_approvals:
                state.pending_approvals.remove(key)
            if requires_approval and not already_approved:
                record = ApprovalRecord(step_id=key, actor=self._approval_actor)
                state.approvals.append(record)
                self._emit(store, state, "gate.approved", key, actor=record.actor, approved_at=record.approved_at)
            local_state.approval_required = False
            effective_isolation = self._effective_isolation(nested, plan)
            local_state.status = StepStatus.RUNNING
            local_state.started_at = local_state.started_at or now()
            store.save_state(state)
            self._emit(
                store, state, "foreach.step.started", key, parent=parent.id, index=index,
                type=nested.type, isolation=effective_isolation,
            )
            try:
                out = await self._execute_foreach_nested_with_retry(
                    nested, plan, state, store, budget, local_state, local, parent, index, item, key
                )
                self._validate_output_schema(nested, out, plan)
                local_state.status = StepStatus.COMPLETED
                local_state.output = out
                local_state.error = None
                local_state.finished_at = now()
                local[nested.id] = self._local_view(local_state)
                outputs[nested.id] = out
                store.save_state(state)
                self._emit(store, state, "foreach.step.completed", key, parent=parent.id, index=index)
            except (ApprovalRequired, CancellationRequested, IntegrityError, ReconciliationRequired, BudgetExceeded):
                store.save_state(state)
                raise
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                local_state.error = err
                local_state.finished_at = now()
                if nested.on_fail == "skip":
                    local_state.status = StepStatus.SKIPPED
                    local[nested.id] = self._local_view(local_state)
                    store.save_state(state)
                    self._emit(store, state, "foreach.step.skipped", key, parent=parent.id, index=index, reason=err)
                    continue
                if nested.on_fail == "continue":
                    local_state.status = StepStatus.FAILED
                    local[nested.id] = self._local_view(local_state)
                    store.save_state(state)
                    self._emit(store, state, "foreach.step.failed_continued", key, parent=parent.id, index=index, error=err)
                    continue
                local_state.status = StepStatus.FAILED
                store.save_state(state)
                raise
        return {"index": index, "item": redact(item), "outputs": outputs}

    async def _execute_foreach_nested_with_retry(
        self, step, plan, state, store, budget, local_state, local, parent, index, item, nested_key
    ):
        attempts = max(1, step.retry.max_attempts)
        last: Exception | None = None
        prefix = self._foreach_item_prefix(parent.id, index, item)
        extra_context = {parent.item_var: item, "foreach_index": index}
        for i in range(attempts):
            self._check_cancelled()
            budget.check(state)
            budget.check_can_invoke(state, step.type)
            local_state.attempts += 1
            self._increment_invocation_usage(step, state, local_state)
            attempt = i + 1
            control = {
                "run_id": state.run_id,
                "step_id": nested_key,
                "logical_step_id": step.id,
                "parent_step_id": parent.id,
                "foreach_index": index,
                "attempt": attempt,
                "idempotency_key": local_state.idempotency_key,
                "isolation": self._effective_isolation(step, plan),
            }
            context = self._context(
                plan, state, extra_steps=dict(local), control=control, extra_context=extra_context
            )
            params = resolve_secret_references(render(step.with_, context), self.secret_resolver)
            self._emit(store, state, "foreach.step.attempt_started", nested_key, parent=parent.id, index=index, attempt=attempt)
            invocation = self._begin_invocation(step, state, local_state, store, step_id=nested_key, attempt=attempt)
            if invocation is not None:
                control["invocation_id"] = invocation.invocation_id
                context = self._context(
                    plan, state, extra_steps=dict(local), control=control, extra_context=extra_context
                )
                params = resolve_secret_references(render(step.with_, context), self.secret_resolver)
            try:
                timeout = budget.effective_step_timeout(step.timeout_seconds, state)
                if timeout is not None and timeout <= 0:
                    raise BudgetExceeded("no run time budget remains")

                async def invoke_and_consume():
                    raw = await self._execute_nested_once(
                        step, params, context, plan, state, store, budget, local, parent.id, prefix
                    )
                    return await self._consume_runtime_output(raw, state, local_state, store, nested_key, invocation)

                out = await self._await_with_controls(invoke_and_consume(), timeout)
                out = redact_secret_values(out)
                self._finish_invocation(invocation, state, store, output=out)
                budget.check(state)
                return out
            except asyncio.TimeoutError:
                timeout_error = TimeoutError(f"foreach step attempt timed out after {timeout}s")
                if step.side_effects in {"local", "external", "destructive"}:
                    last = SideEffectOutcomeUnknown(str(timeout_error))
                    self._finish_invocation(invocation, state, store, error=last, uncertain=True)
                else:
                    last = timeout_error
                    self._finish_invocation(invocation, state, store, error=last)
            except LeaseConflict as exc:
                self._finish_invocation(
                    invocation, state, store, error=exc, uncertain=step.side_effects in {"local", "external", "destructive"}
                )
                raise
            except (CancellationRequested, asyncio.CancelledError) as exc:
                local_state.status = StepStatus.CANCELLED
                self._finish_invocation(
                    invocation, state, store, error=exc,
                    cancelled=step.side_effects == "none",
                    uncertain=step.side_effects in {"local", "external", "destructive"},
                )
                raise
            except Exception as exc:
                last = exc
                self._finish_invocation(invocation, state, store, error=exc)
            self._emit(store, state, "foreach.step.attempt_failed", nested_key, parent=parent.id, index=index, attempt=attempt, error=str(last))
            if attempt >= attempts or not self._should_retry(step, last):
                break
            delay = min(
                step.retry.backoff_seconds * (step.retry.backoff_multiplier ** (attempt - 1)),
                step.retry.max_backoff_seconds,
            )
            if delay:
                await self._await_with_controls(asyncio.sleep(delay), delay + 1.0)
        assert last is not None
        raise last

    def _loop_iteration_prefix(self, parent_id: str, iteration: int) -> str:
        return f"{parent_id}/iteration-{iteration:04d}"

    async def _execute_while(self, step, plan, state, store, budget, base_context):
        limit = step.max_iterations or plan.execution.max_loop_iterations
        limit = min(limit, plan.execution.max_loop_iterations)
        history: list[dict[str, Any]] = []
        for iteration in range(limit):
            self._check_cancelled()
            previous = history[-1] if history else None
            loop_ctx = {"iteration": iteration, "previous": previous, "history": history}
            context = self._context(plan, state, extra_context={"loop": loop_ctx})
            if not bool(safe_eval(step.condition, context)):
                return {
                    "iterations": history,
                    "count": len(history),
                    "last": previous,
                    "terminated": "condition_false",
                }
            result = await self._execute_loop_iteration(
                parent=step,
                iteration=iteration,
                previous=previous,
                history=history,
                plan=plan,
                state=state,
                store=store,
                budget=budget,
            )
            history.append(result)

        previous = history[-1] if history else None
        context = self._context(
            plan,
            state,
            extra_context={"loop": {"iteration": limit, "previous": previous, "history": history}},
        )
        if bool(safe_eval(step.condition, context)):
            raise StepExecutionError(
                f"while step {step.id} exhausted max_iterations={limit} while condition remained true"
            )
        return {
            "iterations": history,
            "count": len(history),
            "last": previous,
            "terminated": "condition_false_at_limit",
        }

    async def _execute_loop_iteration(
        self, *, parent, iteration, previous, history, plan, state, store, budget
    ):
        prefix = self._loop_iteration_prefix(parent.id, iteration)
        local: dict[str, dict[str, Any]] = {}
        outputs: dict[str, Any] = {}
        remaining: dict[str, StepSpec] = {}
        extra_context = {
            "loop": {"iteration": iteration, "previous": previous, "history": history}
        }

        for nested in parent.loop_steps:
            key = f"{prefix}/{nested.id}"
            nested_state = state.nested_steps.get(key)
            if nested_state is None:
                nested_state = StepState(id=key)
                state.nested_steps[key] = nested_state
            if nested_state.status in {StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.FAILED}:
                local[nested.id] = self._local_view(nested_state)
                if nested_state.status == StepStatus.COMPLETED:
                    outputs[nested.id] = nested_state.output
            else:
                remaining[nested.id] = nested
        store.save_state(state)

        while remaining:
            self._check_cancelled()
            ready = []
            for nested in remaining.values():
                internal_needs = [d for d in nested.needs if d in local or d in remaining]
                if all(d in local and local[d]["status"] in {"COMPLETED", "SKIPPED", "FAILED"} for d in internal_needs):
                    ready.append(nested)
            if not ready:
                raise StepExecutionError(
                    f"while iteration {iteration} made no progress; dependency validation drift"
                )

            nested = ready[0]
            del remaining[nested.id]
            key = f"{prefix}/{nested.id}"
            local_state = state.nested_steps[key]
            context = self._context(plan, state, extra_steps=dict(local), extra_context=extra_context)

            if nested.when and not bool(safe_eval(nested.when, context)):
                local_state.status = StepStatus.SKIPPED
                local_state.finished_at = now()
                local[nested.id] = self._local_view(local_state)
                store.save_state(state)
                self._emit(
                    store, state, "while.step.skipped", key,
                    parent=parent.id, iteration=iteration, reason="when=false",
                )
                continue

            policy = PolicyEngine(plan.policy)
            policy.check_step(nested)
            if local_state.idempotency_key is None and nested.idempotency_key:
                rendered_key = render(nested.idempotency_key, context)
                if not isinstance(rendered_key, (str, int, float, bool)):
                    raise StepExecutionError(f"idempotency_key for {key} must render to a scalar")
                local_state.idempotency_key = str(rendered_key)

            requires_approval = policy.approval_required(nested) or (
                nested.type == "gate" and nested.gate == "human"
            )
            already_approved = any(x.step_id == key for x in state.approvals)
            if requires_approval and not already_approved and key not in self._active_approvals:
                local_state.status = StepStatus.WAITING_APPROVAL
                local_state.approval_required = True
                if key not in state.pending_approvals:
                    state.pending_approvals.append(key)
                store.save_state(state)
                raise ApprovalRequired(key)
            if key in state.pending_approvals:
                state.pending_approvals.remove(key)
            if requires_approval and not already_approved:
                record = ApprovalRecord(step_id=key, actor=self._approval_actor)
                state.approvals.append(record)
                self._emit(
                    store, state, "gate.approved", key,
                    actor=record.actor, approved_at=record.approved_at,
                )
            local_state.approval_required = False
            effective_isolation = self._effective_isolation(nested, plan)
            local_state.status = StepStatus.RUNNING
            local_state.started_at = local_state.started_at or now()
            store.save_state(state)
            self._emit(
                store, state, "while.step.started", key,
                parent=parent.id, iteration=iteration, type=nested.type,
                isolation=effective_isolation,
            )
            try:
                out = await self._execute_loop_nested_with_retry(
                    nested, plan, state, store, budget, local_state, local,
                    parent, iteration, previous, history, key
                )
                self._validate_output_schema(nested, out, plan)
                local_state.status = StepStatus.COMPLETED
                local_state.output = out
                local_state.error = None
                local_state.finished_at = now()
                local[nested.id] = self._local_view(local_state)
                outputs[nested.id] = out
                store.save_state(state)
                self._emit(
                    store, state, "while.step.completed", key,
                    parent=parent.id, iteration=iteration,
                )
            except (ApprovalRequired, CancellationRequested, IntegrityError, ReconciliationRequired, BudgetExceeded):
                store.save_state(state)
                raise
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                local_state.error = err
                local_state.finished_at = now()
                if nested.on_fail == "skip":
                    local_state.status = StepStatus.SKIPPED
                    local[nested.id] = self._local_view(local_state)
                    store.save_state(state)
                    self._emit(
                        store, state, "while.step.skipped", key,
                        parent=parent.id, iteration=iteration, reason=err,
                    )
                    continue
                if nested.on_fail == "continue":
                    local_state.status = StepStatus.FAILED
                    local[nested.id] = self._local_view(local_state)
                    store.save_state(state)
                    self._emit(
                        store, state, "while.step.failed_continued", key,
                        parent=parent.id, iteration=iteration, error=err,
                    )
                    continue
                local_state.status = StepStatus.FAILED
                store.save_state(state)
                raise
        return {"iteration": iteration, "outputs": outputs}

    async def _execute_loop_nested_with_retry(
        self, step, plan, state, store, budget, local_state, local,
        parent, iteration, previous, history, nested_key
    ):
        attempts = max(1, step.retry.max_attempts)
        last: Exception | None = None
        prefix = self._loop_iteration_prefix(parent.id, iteration)
        extra_context = {
            "loop": {"iteration": iteration, "previous": previous, "history": history}
        }
        for i in range(attempts):
            self._check_cancelled()
            budget.check(state)
            budget.check_can_invoke(state, step.type)
            local_state.attempts += 1
            self._increment_invocation_usage(step, state, local_state)
            attempt = i + 1
            control = {
                "run_id": state.run_id,
                "step_id": nested_key,
                "logical_step_id": step.id,
                "parent_step_id": parent.id,
                "loop_iteration": iteration,
                "attempt": attempt,
                "idempotency_key": local_state.idempotency_key,
                "isolation": self._effective_isolation(step, plan),
            }
            context = self._context(
                plan, state, extra_steps=dict(local), control=control,
                extra_context=extra_context,
            )
            params = resolve_secret_references(render(step.with_, context), self.secret_resolver)
            self._emit(
                store, state, "while.step.attempt_started", nested_key,
                parent=parent.id, iteration=iteration, attempt=attempt,
            )
            invocation = self._begin_invocation(
                step, state, local_state, store, step_id=nested_key, attempt=attempt
            )
            if invocation is not None:
                control["invocation_id"] = invocation.invocation_id
                context = self._context(
                    plan, state, extra_steps=dict(local), control=control,
                    extra_context=extra_context,
                )
                params = resolve_secret_references(render(step.with_, context), self.secret_resolver)
            try:
                timeout = budget.effective_step_timeout(step.timeout_seconds, state)
                if timeout is not None and timeout <= 0:
                    raise BudgetExceeded("no run time budget remains")

                async def invoke_and_consume():
                    raw = await self._execute_nested_once(
                        step, params, context, plan, state, store, budget, local,
                        parent.id, prefix,
                    )
                    return await self._consume_runtime_output(
                        raw, state, local_state, store, nested_key, invocation
                    )

                out = await self._await_with_controls(invoke_and_consume(), timeout)
                out = redact_secret_values(out)
                self._finish_invocation(invocation, state, store, output=out)
                budget.check(state)
                return out
            except asyncio.TimeoutError:
                timeout_error = TimeoutError(f"while step attempt timed out after {timeout}s")
                if step.side_effects in {"local", "external", "destructive"}:
                    last = SideEffectOutcomeUnknown(str(timeout_error))
                    self._finish_invocation(invocation, state, store, error=last, uncertain=True)
                else:
                    last = timeout_error
                    self._finish_invocation(invocation, state, store, error=last)
            except LeaseConflict as exc:
                self._finish_invocation(
                    invocation, state, store, error=exc,
                    uncertain=step.side_effects in {"local", "external", "destructive"},
                )
                raise
            except (CancellationRequested, asyncio.CancelledError) as exc:
                local_state.status = StepStatus.CANCELLED
                self._finish_invocation(
                    invocation, state, store, error=exc,
                    cancelled=step.side_effects == "none",
                    uncertain=step.side_effects in {"local", "external", "destructive"},
                )
                raise
            except Exception as exc:
                last = exc
                self._finish_invocation(invocation, state, store, error=exc)
            self._emit(
                store, state, "while.step.attempt_failed", nested_key,
                parent=parent.id, iteration=iteration, attempt=attempt, error=str(last),
            )
            if attempt >= attempts or not self._should_retry(step, last):
                break
            delay = min(
                step.retry.backoff_seconds * (step.retry.backoff_multiplier ** (attempt - 1)),
                step.retry.max_backoff_seconds,
            )
            if delay:
                await self._await_with_controls(asyncio.sleep(delay), delay + 1.0)
        assert last is not None
        raise last

    async def _execute_parallel(self, step, plan, state, store, budget, base_context):
        semaphore = asyncio.Semaphore(plan.execution.max_parallel)

        async def run_branch(name, branch_steps):
            async with semaphore:
                return name, await self._execute_parallel_branch(
                    parent=step,
                    branch=name,
                    branch_steps=branch_steps,
                    plan=plan,
                    state=state,
                    store=store,
                    budget=budget,
                    base_context=base_context,
                )

        results = await asyncio.gather(
            *(run_branch(name, bsteps) for name, bsteps in step.branches.items()),
            return_exceptions=True,
        )
        pairs = []
        errors = []
        for item in results:
            if isinstance(item, BaseException):
                errors.append(item)
            else:
                pairs.append(item)
        if errors:
            # Approval/cancellation/integrity errors outrank ordinary branch failures.
            for cls in (ApprovalRequired, CancellationRequested, IntegrityError, ReconciliationRequired, BudgetExceeded):
                for error in errors:
                    if isinstance(error, cls):
                        raise error
            raise errors[0]
        return dict(pairs)

    def _nested_key(self, parent_id: str, branch: str, step_id: str) -> str:
        return f"{parent_id}/{branch}/{step_id}"

    def _local_view(self, step_state: StepState) -> dict[str, Any]:
        return {
            "status": step_state.status.value,
            "output": step_state.output,
            "error": step_state.error,
            "attempts": step_state.attempts,
        }

    async def _execute_parallel_branch(self, *, parent, branch, branch_steps, plan, state, store, budget, base_context):
        local: dict[str, dict[str, Any]] = {}
        outputs: dict[str, Any] = {}
        remaining: dict[str, StepSpec] = {}

        for nested in branch_steps:
            key = self._nested_key(parent.id, branch, nested.id)
            nested_state = state.nested_steps.get(key)
            if nested_state is None:
                nested_state = StepState(id=key)
                state.nested_steps[key] = nested_state
            if nested_state.status in {StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.FAILED}:
                local[nested.id] = self._local_view(nested_state)
                if nested_state.status == StepStatus.COMPLETED:
                    outputs[nested.id] = nested_state.output
            else:
                remaining[nested.id] = nested
        store.save_state(state)

        while remaining:
            self._check_cancelled()
            ready = []
            for nested in remaining.values():
                internal_needs = [d for d in nested.needs if d in local or d in remaining]
                if all(d in local and local[d]["status"] in {"COMPLETED", "SKIPPED", "FAILED"} for d in internal_needs):
                    ready.append(nested)
            if not ready:
                raise StepExecutionError(f"parallel branch {branch} made no progress; dependency validation drift")

            nested = ready[0]
            del remaining[nested.id]
            key = self._nested_key(parent.id, branch, nested.id)
            local_state = state.nested_steps[key]
            extra = dict(local)
            context = self._context(plan, state, extra_steps=extra)

            if nested.when and not bool(safe_eval(nested.when, context)):
                local_state.status = StepStatus.SKIPPED
                local_state.finished_at = now()
                local[nested.id] = self._local_view(local_state)
                store.save_state(state)
                self._emit(store, state, "parallel.step.skipped", key, parent=parent.id, branch=branch, reason="when=false")
                continue

            policy = PolicyEngine(plan.policy)
            policy.check_step(nested)
            if local_state.idempotency_key is None and nested.idempotency_key:
                rendered_key = render(nested.idempotency_key, context)
                if not isinstance(rendered_key, (str, int, float, bool)):
                    raise StepExecutionError(f"idempotency_key for {key} must render to a scalar")
                local_state.idempotency_key = str(rendered_key)

            requires_approval = policy.approval_required(nested) or (nested.type == "gate" and nested.gate == "human")
            already_approved = any(x.step_id == key for x in state.approvals)
            if requires_approval and not already_approved and key not in self._active_approvals:
                local_state.status = StepStatus.WAITING_APPROVAL
                local_state.approval_required = True
                if key not in state.pending_approvals:
                    state.pending_approvals.append(key)
                store.save_state(state)
                raise ApprovalRequired(key)
            if key in state.pending_approvals:
                state.pending_approvals.remove(key)
            if requires_approval and not already_approved:
                record = ApprovalRecord(step_id=key, actor=self._approval_actor)
                state.approvals.append(record)
                self._emit(store, state, "gate.approved", key, actor=record.actor, approved_at=record.approved_at)
            local_state.approval_required = False
            effective_isolation = self._effective_isolation(nested, plan)

            local_state.status = StepStatus.RUNNING
            local_state.started_at = local_state.started_at or now()
            store.save_state(state)
            self._emit(
                store,
                state,
                "parallel.step.started",
                key,
                parent=parent.id,
                branch=branch,
                type=nested.type,
                isolation=effective_isolation,
            )
            try:
                out = await self._execute_nested_with_retry(
                    nested, plan, state, store, budget, local_state, local, parent.id, branch, key
                )
                self._validate_output_schema(nested, out, plan)
                local_state.status = StepStatus.COMPLETED
                local_state.output = out
                local_state.error = None
                local_state.finished_at = now()
                local[nested.id] = self._local_view(local_state)
                outputs[nested.id] = out
                store.save_state(state)
                self._emit(store, state, "parallel.step.completed", key, parent=parent.id, branch=branch)
            except (ApprovalRequired, CancellationRequested, IntegrityError, ReconciliationRequired, BudgetExceeded):
                store.save_state(state)
                raise
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                local_state.error = err
                local_state.finished_at = now()
                if nested.on_fail == "skip":
                    local_state.status = StepStatus.SKIPPED
                    local[nested.id] = self._local_view(local_state)
                    store.save_state(state)
                    self._emit(store, state, "parallel.step.skipped", key, parent=parent.id, branch=branch, reason=err)
                    continue
                if nested.on_fail == "continue":
                    local_state.status = StepStatus.FAILED
                    local[nested.id] = self._local_view(local_state)
                    store.save_state(state)
                    self._emit(store, state, "parallel.step.failed_continued", key, parent=parent.id, branch=branch, error=err)
                    continue
                local_state.status = StepStatus.FAILED
                store.save_state(state)
                raise
        return outputs

    async def _execute_nested_with_retry(
        self, step, plan, state, store, budget, local_state, local, parent_id, branch, nested_key
    ):
        attempts = max(1, step.retry.max_attempts)
        last: Exception | None = None
        for i in range(attempts):
            self._check_cancelled()
            budget.check(state)
            budget.check_can_invoke(state, step.type)
            local_state.attempts += 1
            self._increment_invocation_usage(step, state, local_state)
            attempt = i + 1
            extra = dict(local)
            control = {
                "run_id": state.run_id,
                "step_id": nested_key,
                "logical_step_id": step.id,
                "parent_step_id": parent_id,
                "parallel_branch": branch,
                "attempt": attempt,
                "idempotency_key": local_state.idempotency_key,
                "isolation": self._effective_isolation(step, plan),
            }
            context = self._context(plan, state, extra_steps=extra, control=control)
            params = resolve_secret_references(render(step.with_, context), self.secret_resolver)
            self._emit(store, state, "parallel.step.attempt_started", nested_key, parent=parent_id, branch=branch, attempt=attempt)
            invocation = self._begin_invocation(
                step, state, local_state, store, step_id=nested_key, attempt=attempt
            )
            if invocation is not None:
                control["invocation_id"] = invocation.invocation_id
                context = self._context(plan, state, extra_steps=extra, control=control)
                params = resolve_secret_references(render(step.with_, context), self.secret_resolver)
            try:
                timeout = budget.effective_step_timeout(step.timeout_seconds, state)
                if timeout is not None and timeout <= 0:
                    raise BudgetExceeded("no run time budget remains")
                async def invoke_and_consume_nested():
                    raw = await self._execute_nested_once(step, params, context, plan, state, store, budget, local, parent_id, branch)
                    return await self._consume_runtime_output(raw, state, local_state, store, nested_key, invocation)

                out = await self._await_with_controls(invoke_and_consume_nested(), timeout)
                out = redact_secret_values(out)
                self._finish_invocation(invocation, state, store, output=out)
                budget.check(state)
                return out
            except asyncio.TimeoutError:
                timeout_error = TimeoutError(f"parallel step attempt timed out after {timeout}s")
                if step.side_effects in {"local", "external", "destructive"}:
                    last = SideEffectOutcomeUnknown(str(timeout_error))
                    self._finish_invocation(invocation, state, store, error=last, uncertain=True)
                else:
                    last = timeout_error
                    self._finish_invocation(invocation, state, store, error=last)
            except LeaseConflict as exc:
                if step.side_effects in {"local", "external", "destructive"}:
                    self._finish_invocation(invocation, state, store, error=exc, uncertain=True)
                else:
                    self._finish_invocation(invocation, state, store, error=exc)
                raise
            except (CancellationRequested, asyncio.CancelledError) as exc:
                local_state.status = StepStatus.CANCELLED
                self._finish_invocation(
                    invocation,
                    state,
                    store,
                    error=exc,
                    cancelled=step.side_effects == "none",
                    uncertain=step.side_effects in {"local", "external", "destructive"},
                )
                raise
            except Exception as exc:
                last = exc
                self._finish_invocation(invocation, state, store, error=exc)
            self._emit(store, state, "parallel.step.attempt_failed", nested_key, parent=parent_id, branch=branch, attempt=attempt, error=str(last))
            if attempt >= attempts or not self._should_retry(step, last):
                break
            delay = min(
                step.retry.backoff_seconds * (step.retry.backoff_multiplier ** (attempt - 1)),
                step.retry.max_backoff_seconds,
            )
            if delay:
                await self._await_with_controls(asyncio.sleep(delay), delay + 1.0)
        assert last is not None
        raise last

    async def _execute_nested_once(self, step, params, context, plan, state, store, budget, local, parent_id, branch):
        if step.type == "skill":
            return await self.runtime.execute_skill(step, params, context)
        if step.type == "action":
            return await self.runtime.execute_action(step, params, context)
        if step.type == "agent":
            return await self.runtime.execute_agent(step, params, context)
        if step.type == "eval":
            out = await self.runtime.execute_eval(step, params, context)
            candidate = out.value if isinstance(out, RuntimeResult) else out
            if isinstance(candidate, dict) and candidate.get("passed") is False:
                raise StepExecutionError(f"eval failed: {candidate}")
            return out
        if step.type == "gate":
            if step.gate == "human":
                return {"approved": True}
            if step.gate == "budget":
                budget.check(state)
                return {"passed": True, "gate": "budget"}
            if step.condition and not bool(safe_eval(step.condition, context)):
                raise StepExecutionError(f"gate condition failed: {step.condition}")
            return {"passed": True, "gate": step.gate}
        if step.type == "branch":
            for case in step.cases:
                if bool(safe_eval(case.when, context)):
                    return {"selected": render(case.value, context)}
            return {"selected": render(step.default, context)}
        if step.type == "playbook":
            base = (Path(plan.source_path).parent if plan.source_path else Path.cwd()).resolve()
            child_path = (base / step.playbook).resolve()
            if not child_path.is_relative_to(base):
                raise StepExecutionError(f"subplaybook path escapes playbook directory: {step.playbook}")
            child_pb = load_playbook(child_path)
            child_plan = self.compiler.compile(child_pb, params, source_path=str(child_path))
            child_dir = store.run_dir / "subruns" / parent_id / branch / step.id
            child_state = await Runner(
                self.runtime, self.compiler, telemetry=self.telemetry, schema_resolver=self.schema_resolver,
                secret_resolver=self.secret_resolver, store_factory=self.store_factory,
                lease_ttl_seconds=self.lease_ttl_seconds, lease_heartbeat_seconds=self.lease_heartbeat_seconds,
                enforce_capabilities=self.enforce_capabilities,
            ).run(
                child_plan, child_dir, parent_run_id=state.run_id,
                root_run_id=state.root_run_id or state.run_id,
                fork_reason=f"subplaybook:{step.id}",
                parent_lineage_depth=state.lineage_depth,
            )
            if child_state.status != RunStatus.COMPLETED:
                raise StepExecutionError(f"subplaybook {step.id} ended as {child_state.status.value}")
            state.usage.add(child_state.usage)
            return child_state.outputs
        raise StepExecutionError(f"nested step type not supported: {step.type}")
