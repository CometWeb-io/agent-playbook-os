import pytest
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.errors import CompileError, PolicyDenied
from agent_playbook_os.models import Playbook


def pb(steps, inputs=None, deps=None, policy=None):
    return Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1",
        "kind":"Playbook",
        "metadata":{"id":"test","version":"0.1.0","description":"test playbook"},
        "spec":{
            "inputs": inputs or {},
            "dependencies": {"skills": deps or {}},
            "policy": policy or {},
            "steps": steps,
        }
    })


def test_compiles_dag_and_reorders():
    p = pb([
        {"id":"b","type":"action","action":"set","needs":["a"]},
        {"id":"a","type":"action","action":"set"},
    ])
    plan = Compiler().compile(p)
    assert [x.spec.id for x in plan.steps] == ["a","b"]


def test_cycle_rejected():
    p = pb([
        {"id":"a","type":"action","action":"set","needs":["b"]},
        {"id":"b","type":"action","action":"set","needs":["a"]},
    ])
    with pytest.raises(CompileError, match="cycle"):
        Compiler().compile(p)


def test_undeclared_data_dependency_rejected():
    p = pb([
        {"id":"a","type":"action","action":"set"},
        {"id":"b","type":"action","action":"set","with":{"value":"{{ steps.a.output }}"}},
    ])
    with pytest.raises(CompileError, match="does not declare"):
        Compiler().compile(p)


def test_required_input():
    p = pb([{"id":"a","type":"action","action":"set"}], inputs={"x":{"type":"string","required":True}})
    with pytest.raises(CompileError, match="missing required"):
        Compiler().compile(p)


def test_skill_must_be_declared():
    p = pb([{"id":"a","type":"skill","uses":"foo"}])
    with pytest.raises(CompileError, match="not declared"):
        Compiler().compile(p)


def test_policy_denied_action():
    p = pb([{"id":"a","type":"action","action":"command"}], policy={"denied_actions":["command"]})
    with pytest.raises(PolicyDenied):
        Compiler().compile(p)

def test_nested_parallel_skill_must_be_declared():
    p = pb([{"id":"p","type":"parallel","branches":{"a":[{"id":"s","type":"skill","uses":"nested-skill"}]}}])
    with pytest.raises(CompileError, match="not declared"):
        Compiler().compile(p)


def test_policy_applies_to_nested_parallel_skill():
    p = pb(
        [{"id":"p","type":"parallel","branches":{"a":[{"id":"s","type":"skill","uses":"nested-skill"}]}}],
        deps={"nested-skill":{"source":"local"}},
        policy={"denied_skills":["nested-skill"]},
    )
    with pytest.raises(PolicyDenied):
        Compiler().compile(p)


def test_semantic_hash_is_stable_across_recompilation():
    p = pb([{"id":"a","type":"action","action":"set","with":{"value":1}}])
    a = Compiler().compile(p, source_path="/tmp/a/playbook.yaml")
    b = Compiler().compile(p, source_path="/tmp/b/playbook.yaml")
    assert a.semantic_hash == b.semantic_hash
    assert a.playbook_hash == b.playbook_hash
    assert a.integrity_hash != b.integrity_hash


def test_policy_can_require_resolved_skill_locks():
    p = pb(
        [{"id":"s","type":"skill","uses":"foo"}],
        deps={"foo":{"source":"github:example/skills"}},
        policy={"require_resolved_skills": True},
    )
    with pytest.raises(PolicyDenied, match="resolved skill locks"):
        Compiler().compile(p)
