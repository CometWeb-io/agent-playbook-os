import json
from agent_playbook_os.skills import SkillResolver


def test_indexes_plain_agent_skill(tmp_path):
    d = tmp_path / "skills" / "foo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: foo\ndescription: Example skill for testing.\n---\n# Foo\n")
    (d / "VERSION").write_text("1.2.3\n")
    r = SkillResolver([tmp_path])
    x = r.resolve("foo")
    assert x.version == "1.2.3"
    assert x.content_hash.startswith("sha256:")


def test_indexes_cometweb_registry(tmp_path):
    (tmp_path / "registry").mkdir()
    (tmp_path / "skills" / "foo").mkdir(parents=True)
    (tmp_path / "skills" / "foo" / "SKILL.md").write_text("---\nname: foo\ndescription: Registry skill.\n---\n")
    (tmp_path / "registry" / "skills.json").write_text(json.dumps({"skills":[{"id":"foo","version":"2.0.0","description":"x","inputs":["a"],"outputs":["b"],"compatible_hosts":["cursor"]}]}))
    x = SkillResolver([tmp_path]).resolve("foo")
    assert x.outputs == ["b"]
    assert x.compatible_hosts == ["cursor"]
