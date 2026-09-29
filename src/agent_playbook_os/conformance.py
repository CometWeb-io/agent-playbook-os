from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import StepSpec


class ConformanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    passed: bool
    detail: str | None = None


class ConformanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    checks: list[ConformanceCheck] = Field(default_factory=list)


async def run_runtime_conformance(runtime: Any) -> ConformanceReport:
    checks: list[ConformanceCheck] = []
    for method in ["execute_skill", "execute_action", "execute_agent", "execute_eval"]:
        checks.append(ConformanceCheck(
            name=f"method:{method}",
            passed=callable(getattr(runtime, method, None)),
            detail=None if callable(getattr(runtime, method, None)) else "missing callable",
        ))

    caps_fn = getattr(runtime, "capabilities", None)
    if callable(caps_fn):
        try:
            registry = caps_fn()
            ids = [x.id for x in registry.list()]
            checks.append(ConformanceCheck(
                name="capabilities:unique",
                passed=len(ids) == len(set(ids)),
                detail=f"count={len(ids)}",
            ))
        except Exception as exc:
            checks.append(ConformanceCheck(name="capabilities:read", passed=False, detail=str(exc)))
    else:
        checks.append(ConformanceCheck(name="capabilities:read", passed=False, detail="missing capabilities()"))

    step = StepSpec(id="conformance-set", type="action", action="set", with_={"value": {"ok": True}})
    try:
        out = await runtime.execute_action(step, {"value": {"ok": True}}, {"control": {"attempt": 1}})
        checks.append(ConformanceCheck(name="action:set", passed=out == {"ok": True}, detail=repr(out)))
    except Exception as exc:
        checks.append(ConformanceCheck(name="action:set", passed=False, detail=f"{type(exc).__name__}: {exc}"))

    for mode in ["inline", "sandbox", "subagent"]:
        fn = getattr(runtime, "supports_isolation", None)
        if callable(fn):
            try:
                result = fn(mode, step)
                checks.append(ConformanceCheck(
                    name=f"isolation:{mode}:boolean",
                    passed=isinstance(result, bool),
                    detail=str(result),
                ))
            except Exception as exc:
                checks.append(ConformanceCheck(name=f"isolation:{mode}:boolean", passed=False, detail=str(exc)))
        else:
            checks.append(ConformanceCheck(name=f"isolation:{mode}:boolean", passed=False, detail="missing supports_isolation"))

    passed = all(x.passed for x in checks)
    return ConformanceReport(passed=passed, checks=checks)
