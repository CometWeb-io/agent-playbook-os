"""Pinned child graph integrity: compile before execution and refuse source drift."""
import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import CompileError, IntegrityError
from agent_playbook_os.integrity import canonical_hash, compiled_plan_integrity
from agent_playbook_os.models import Playbook, RunStatus
from agent_playbook_os.runtime import ReferenceRuntime


def write_child(path, *, child=None, value="ok"):
    steps = (
        [{"id": "inner", "type": "playbook", "playbook": child}]
        if child else [{"id": "set", "type": "action", "action": "set",
                        "with": {"value": value}}]
    )
    import yaml
    path.write_text(yaml.safe_dump({
        "apiVersion": "playbook.agent/v1alpha1",
        "kind": "Playbook",
        "metadata": {"id": path.stem.replace("_", "-"), "version": "0.1.0",
                     "description": "test dependency"},
        "spec": {"steps": steps},
    }), encoding="utf-8")


def parent_playbook():
    return Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "root", "version": "0.1.0", "description": "root"},
        "spec": {"steps": [
            {"id": "child", "type": "playbook", "playbook": "child.yaml"}
        ]},
    })


def make_graph(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write_child(root / "child.yaml", child="grandchild.yaml")
    write_child(root / "grandchild.yaml")
    source = root / "main.yaml"
    source.write_text("# source marker", encoding="utf-8")
    return root, source


@pytest.mark.asyncio
async def test_complete_child_graph_is_locked_and_executes(tmp_path):
    root, source = make_graph(tmp_path)
    plan = Compiler().compile(parent_playbook(), source_path=str(source))
    assert sorted(plan.subplaybook_locks) == ["child.yaml", "grandchild.yaml"]
    assert all(x.startswith("sha256:") for x in plan.subplaybook_locks.values())
    assert plan.integrity_hash == compiled_plan_integrity(plan)
    done = await Runner(ReferenceRuntime()).run(plan, tmp_path / "run")
    assert done.status == RunStatus.COMPLETED
    assert (tmp_path / "run" / "subruns" / "child" / "subruns" / "inner" / "state.json").exists()


@pytest.mark.asyncio
async def test_grandchild_source_drift_blocks_before_run_creation(tmp_path):
    root, source = make_graph(tmp_path)
    plan = Compiler().compile(parent_playbook(), source_path=str(source))
    write_child(root / "grandchild.yaml", value="changed-after-review")
    dest = tmp_path / "never-run"
    with pytest.raises(IntegrityError, match="dependency changed"):
        await Runner(ReferenceRuntime()).run(plan, dest)
    assert not dest.exists()


@pytest.mark.asyncio
async def test_direct_child_source_drift_blocks_before_run_creation(tmp_path):
    root, source = make_graph(tmp_path)
    plan = Compiler().compile(parent_playbook(), source_path=str(source))
    write_child(root / "child.yaml", value="new-step")
    dest = tmp_path / "never-run"
    with pytest.raises(IntegrityError, match="dependency changed"):
        await Runner(ReferenceRuntime()).run(plan, dest)
    assert not dest.exists()


def test_cycle_in_child_imports_fails_at_compile_time(tmp_path):
    root, source = make_graph(tmp_path)
    write_child(root / "grandchild.yaml", child="child.yaml")
    with pytest.raises(CompileError, match="cyclic subplaybook import"):
        Compiler().compile(parent_playbook(), source_path=str(source))


def test_legacy_compiled_plan_hash_without_child_manifest_stays_valid():
    pb = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "simple", "version": "0.1.0", "description": "simple"},
        "spec": {"steps": [{"id": "x", "type": "action", "action": "set"}]},
    })
    plan = Compiler().compile(pb)
    payload = plan.model_dump(mode="json", by_alias=True)
    payload.pop("subplaybook_locks")
    assert compiled_plan_integrity(payload) == plan.integrity_hash


@pytest.mark.asyncio
async def test_legacy_unlocked_children_are_rejected_before_first_effect(tmp_path):
    root, source = make_graph(tmp_path)
    plan = Compiler().compile(parent_playbook(), source_path=str(source))
    plan.subplaybook_locks = {}
    from agent_playbook_os.integrity import compiled_plan_semantic_hash
    plan.semantic_hash = compiled_plan_semantic_hash(plan)
    plan.integrity_hash = compiled_plan_integrity(plan)
    dest = tmp_path / "legacy"
    with pytest.raises(IntegrityError, match="without dependency locks"):
        await Runner(ReferenceRuntime()).run(plan, dest)
    assert not dest.exists()
