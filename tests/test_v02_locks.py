from pathlib import Path

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.locks import lock_from_plan
from agent_playbook_os.models import Playbook
from agent_playbook_os.skills import SkillResolver


def test_lock_contains_whole_skill_hash(tmp_path):
    root = tmp_path / "skillsrepo"
    skill = root / "skills" / "foo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: foo\ndescription: foo\n---\n")
    (skill / "VERSION").write_text("1.0.0")
    (skill / "scripts").mkdir()
    (skill / "scripts" / "x.py").write_text("print(1)")
    pb = Playbook.model_validate({
        "apiVersion":"playbook.agent/v1alpha1","kind":"Playbook",
        "metadata":{"id":"lock-test","version":"0.2.0","description":"lock"},
        "spec":{
            "dependencies":{"skills":{"foo":{"source":"local"}}},
            "steps":[{"id":"s","type":"skill","uses":"foo"}]
        }
    })
    plan = Compiler(SkillResolver([root]), strict_skill_resolution=True).compile(pb)
    lock = lock_from_plan(plan)
    assert lock.skills["foo"].resolved is True
    assert lock.skills["foo"].content_hash.startswith("sha256:")
    assert lock.plan_integrity_hash == plan.integrity_hash
