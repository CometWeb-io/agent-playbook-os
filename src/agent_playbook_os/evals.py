from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .compiler import Compiler
from .engine import Runner
from .loader import load_playbook
from .runtime import ReferenceRuntime
from .store import RunStore


class EvalExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    compile_error_contains: str | None = None
    outputs: dict[str, Any] | None = None
    step_statuses: dict[str, str] = Field(default_factory=dict)
    event_types_include: list[str] = Field(default_factory=list)
    error_contains: dict[str, str] = Field(default_factory=dict)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    playbook: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    approvals: list[str] = Field(default_factory=list)
    dry_run: bool = False
    expect: EvalExpectation


class EvalCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    passed: bool
    failures: list[str] = Field(default_factory=list)
    run_dir: str | None = None


class EvalSuiteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: str
    total: int
    passed: int
    failed: int
    cases: list[EvalCaseResult]


def load_eval_cases(path: str | Path) -> list[EvalCase]:
    path = Path(path)
    cases = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cases.append(EvalCase.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"invalid eval case at {path}:{lineno}: {exc}") from exc
    return cases


async def run_eval_suite(path: str | Path, run_root: str | Path | None = None) -> EvalSuiteResult:
    suite_path = Path(path).resolve()
    cases = load_eval_cases(suite_path)
    temp = None
    if run_root is None:
        temp = tempfile.TemporaryDirectory(prefix="agent-playbook-evals-")
        root = Path(temp.name)
    else:
        root = Path(run_root).resolve()
        root.mkdir(parents=True, exist_ok=True)

    results: list[EvalCaseResult] = []
    try:
        for index, case in enumerate(cases, 1):
            pb_path = (suite_path.parent / case.playbook).resolve()
            failures = []
            try:
                pb = load_playbook(pb_path)
                plan = Compiler().compile(pb, case.inputs, source_path=str(pb_path))
            except Exception as exc:
                expected = case.expect.compile_error_contains
                if expected is None:
                    failures.append(f"unexpected compile error: {type(exc).__name__}: {exc}")
                elif expected not in str(exc):
                    failures.append(
                        f"compile error does not contain {expected!r}: {type(exc).__name__}: {exc}"
                    )
                results.append(EvalCaseResult(
                    name=case.name,
                    passed=not failures,
                    failures=failures,
                    run_dir=None,
                ))
                continue
            if case.expect.compile_error_contains is not None:
                results.append(EvalCaseResult(
                    name=case.name,
                    passed=False,
                    failures=[
                        f"expected compile error containing {case.expect.compile_error_contains!r}, but compilation succeeded"
                    ],
                    run_dir=None,
                ))
                continue
            if case.expect.status is None:
                results.append(EvalCaseResult(
                    name=case.name,
                    passed=False,
                    failures=["eval expectation must define status when compilation is expected to succeed"],
                    run_dir=None,
                ))
                continue
            case_dir = root / f"{index:03d}-{case.name}"
            state = await Runner(ReferenceRuntime(dry_run=case.dry_run)).run(
                plan, case_dir, approvals=set(case.approvals)
            )
            if state.status.value != case.expect.status:
                failures.append(f"status expected={case.expect.status} actual={state.status.value}")
            if case.expect.outputs is not None and state.outputs != case.expect.outputs:
                failures.append(f"outputs expected={case.expect.outputs!r} actual={state.outputs!r}")
            for sid, expected in case.expect.step_statuses.items():
                actual = state.steps.get(sid)
                actual_status = actual.status.value if actual else None
                if actual_status != expected:
                    failures.append(f"step {sid} status expected={expected} actual={actual_status}")
            events = RunStore(case_dir).read_events()
            event_types = {x.get("type") for x in events}
            for event_type in case.expect.event_types_include:
                if event_type not in event_types:
                    failures.append(f"missing event type: {event_type}")
            for sid, substring in case.expect.error_contains.items():
                actual = state.steps.get(sid)
                error = actual.error if actual else None
                if error is None or substring not in error:
                    failures.append(f"step {sid} error does not contain {substring!r}: {error!r}")
            results.append(EvalCaseResult(
                name=case.name,
                passed=not failures,
                failures=failures,
                run_dir=str(case_dir) if run_root is not None else None,
            ))
    finally:
        if temp is not None:
            temp.cleanup()

    passed = sum(1 for x in results if x.passed)
    return EvalSuiteResult(
        suite=str(suite_path),
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        cases=results,
    )
