from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

import pytest

from agent_playbook_os.errors import ExternalExecutionRequired
from agent_playbook_os.models import RuntimeResult, SkillDependency, StepSpec, Playbook, RunStatus
from agent_playbook_os.skills import SkillResolver
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner

from adapters.agent_skills.contract import (
    AgentSkillsRuntime,
    SkillInvocation,
)


@dataclass
class MemoryHost:
    result: object
    invocation: SkillInvocation | None = None

    async def invoke_skill(self, invocation: SkillInvocation) -> object:
        self.invocation = invocation
        return self.result


def skill_step() -> StepSpec:
    return StepSpec(
        id="run-skill",
        type="skill",
        uses="foo",
        side_effects="external",
    )


@pytest.mark.asyncio
async def test_adapter_rejects_unresolved_skill():
    host = MemoryHost(result={"ok": True})
    runtime = AgentSkillsRuntime(host=host, resolver=SkillResolver(), skill_locks={})

    with pytest.raises(ExternalExecutionRequired, match="unresolved"):
        await runtime.execute_skill(
            skill_step(), {"question": "hello"}, {"control": {"invocation_id": "inv-1"}}
        )
    assert host.invocation is None


@pytest.mark.asyncio
async def test_adapter_passes_locked_skill_and_invocation_id_to_host(tmp_path):
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: foo\n---\n# Foo\n", encoding="utf-8")
    resolver = SkillResolver([tmp_path])
    lock = resolver.lock("foo", SkillDependency(source="local"))
    result = RuntimeResult(value={"answer": 42})
    host = MemoryHost(result=result)
    runtime = AgentSkillsRuntime(host=host, resolver=resolver, skill_locks={"foo": lock})

    returned = await runtime.execute_skill(
        skill_step(),
        {"question": "hello", "nested": {"items": ["x"]}},
        {
            "control": {
                "invocation_id": "inv-1",
                "idempotency_key": "idem-1",
            }
        },
    )

    assert returned is result
    assert host.invocation is not None
    assert host.invocation.skill_id == "foo"
    assert host.invocation.skill_path == str(skill_dir.resolve())
    assert host.invocation.content_hash == lock.content_hash
    assert host.invocation.invocation_id == "inv-1"
    assert host.invocation.side_effects == "external"
    assert host.invocation.idempotency_key == "idem-1"
    assert host.invocation.inputs["question"] == "hello"
    with pytest.raises(TypeError):
        host.invocation.inputs["nested"]["items"] = []


@pytest.mark.asyncio
async def test_adapter_preserves_plain_payload_without_interpreting_domain_status(tmp_path):
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: foo\n---\n# Foo\n", encoding="utf-8")
    resolver = SkillResolver([tmp_path])
    lock = resolver.lock("foo", SkillDependency(source="local"))
    unknown = {"status": "unknown", "reason": "host lost connection"}
    runtime = AgentSkillsRuntime(
        host=MemoryHost(result=unknown), resolver=resolver, skill_locks={"foo": lock}
    )

    returned = await runtime.execute_skill(
        skill_step(), {}, {"control": {"invocation_id": "inv-unknown"}}
    )

    assert returned is unknown


@pytest.mark.asyncio
async def test_locked_skill_runs_through_strict_runner(tmp_path):
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: foo\n---\n# Foo\n", encoding="utf-8")
    resolver = SkillResolver([tmp_path])
    playbook = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "locked", "version": "0.1.0", "description": "locked skill"},
        "spec": {"dependencies": {"skills": {"foo": {"source": "local"}}}, "policy": {"require_resolved_skills": True},
                 "steps": [{"id": "foo", "type": "skill", "uses": "foo"}]},
    })
    plan = Compiler(skill_resolver=resolver, strict_skill_resolution=True).compile(playbook)
    host = MemoryHost(result={"answer": 42})
    runtime = AgentSkillsRuntime(host=host, resolver=resolver, skill_locks=plan.skill_lock)
    state = await Runner(runtime, enforce_capabilities=True).run(plan, tmp_path / "run")
    assert state.status == RunStatus.COMPLETED
    assert state.steps["foo"].output == {"answer": 42}
    assert host.invocation.invocation_id == state.invocations[0].invocation_id


@pytest.mark.asyncio
async def test_dry_run_never_calls_host(tmp_path):
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: foo\n---\n# Foo\n", encoding="utf-8")
    resolver = SkillResolver([tmp_path])
    host = MemoryHost(result={"unexpected": True})
    runtime = AgentSkillsRuntime(host=host, resolver=resolver,
                                skill_locks={"foo": resolver.lock("foo", SkillDependency(source="local"))}, dry_run=True)
    result = await runtime.execute_skill(skill_step(), {}, {"control": {"invocation_id": "dry"}})
    assert host.invocation is None
    assert result["kind"] == "skill-invocation"


def test_built_wheel_exposes_skill_bridge_outside_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "--no-build-isolation", "--no-index", "-w", str(tmp_path)], capture_output=True, text=True, timeout=90)
    assert built.returncode == 0, built.stderr
    wheel = next(tmp_path.glob("agent_playbook_os-*.whl"))
    code = "import sys; sys.path.insert(0, sys.argv[1]); from agent_playbook_os.adapters.agent_skills import AgentSkillsRuntime; from agent_playbook_os.skills import SkillResolver; runtime = AgentSkillsRuntime(host=None, resolver=SkillResolver(), skill_locks={}); assert any(c.kind == 'skill-runtime' for c in runtime.capabilities().list())"
    installed = subprocess.run([sys.executable, "-I", "-c", code, str(wheel)], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert installed.returncode == 0, installed.stderr


@pytest.mark.asyncio
async def test_relocated_resolver_cannot_dispatch_unverified_original_path(tmp_path):
    import shutil

    original = tmp_path / "original" / "skills" / "foo"
    original.mkdir(parents=True)
    (original / "SKILL.md").write_text("---\nname: foo\n---\nApproved instructions")
    lock = SkillResolver([tmp_path / "original"]).lock("foo", SkillDependency(source="local"))
    relocated = tmp_path / "relocated" / "skills" / "foo"
    shutil.copytree(original, relocated)
    (original / "SKILL.md").write_text("Unreviewed replacement")
    resolver = SkillResolver([tmp_path / "relocated"])
    host = MemoryHost(result="should never run")
    runtime = AgentSkillsRuntime(host=host, resolver=resolver, skill_locks={"foo": lock})
    with pytest.raises(ExternalExecutionRequired, match="path"):
        await runtime.execute_skill(skill_step(), {}, {"control": {"invocation_id": "relocated"}})
    assert host.invocation is None
