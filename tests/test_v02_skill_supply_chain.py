import json
import os
import subprocess
import shlex
import sys
import pytest

from agent_playbook_os.errors import ResolutionError
from agent_playbook_os.models import SkillDependency
from agent_playbook_os.skills import SkillResolver


def make_skill(root, name, extra=""):
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test skill\n---\n# {name}\n")
    (d / "VERSION").write_text("1.0.0\n")
    (d / "references").mkdir()
    (d / "references" / "rules.md").write_text("rule" + extra)
    return d


def test_tree_hash_detects_reference_drift(tmp_path):
    skill = make_skill(tmp_path, "foo")
    resolver = SkillResolver([tmp_path])
    lock = resolver.lock("foo", SkillDependency(source="local"))
    assert resolver.verify_lock(lock) == (True, None)
    (skill / "references" / "rules.md").write_text("changed")
    ok, error = resolver.verify_lock(lock)
    assert ok is False
    assert "content hash drift" in error


def test_duplicate_skill_ids_are_ambiguous(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    make_skill(a, "foo", "a")
    make_skill(b, "foo", "b")
    resolver = SkillResolver([a, b])
    with pytest.raises(ResolutionError, match="ambiguous"):
        resolver.resolve("foo")


def test_skill_tree_rejects_symlink(tmp_path):
    root = tmp_path / "repo"
    skill = make_skill(root, "foo")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (skill / "references" / "link.txt").symlink_to(outside)
    with pytest.raises(ResolutionError, match="symlinks"):
        SkillResolver([root])


@pytest.mark.parametrize("version", ["1.0.0", "1.2.3-rc.1+build.4"])
def test_exact_version_is_verified(tmp_path, version):
    skill = make_skill(tmp_path, "foo")
    (skill / "VERSION").write_text(version)
    resolver = SkillResolver([tmp_path])
    lock = resolver.lock("foo", SkillDependency(source="local", version=version))
    assert lock.resolved and lock.version == version
    assert resolver.verify_lock(lock) == (True, None)


@pytest.mark.parametrize("optional", [False, True])
@pytest.mark.parametrize("version", ["999.0.0", ">=1.0.0", "^1.0.0", "1.0", "01.0.0", "1.0.0-01"])
def test_requested_version_is_enforced(tmp_path, version, optional):
    make_skill(tmp_path, "foo")
    resolver = SkillResolver([tmp_path])
    dependency = SkillDependency(source="local", version=version, optional=optional)
    with pytest.raises(ResolutionError, match="version"):
        resolver.resolve("foo", dependency)
    with pytest.raises(ResolutionError, match="version"):
        resolver.lock("foo", dependency)


def test_registry_cannot_replace_missing_or_mismatched_package_version(tmp_path):
    skill = make_skill(tmp_path, "foo")
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / "skills.json").write_text(json.dumps({
        "skills": [{"id": "foo", "version": "2.0.0"}],
    }))
    for missing in (False, True):
        if missing:
            (skill / "VERSION").unlink()
        resolver = SkillResolver([tmp_path])
        with pytest.raises(ResolutionError, match="version"):
            resolver.lock("foo", SkillDependency(source="local", version="2.0.0"))


def test_optional_absence_is_unresolved_but_optional_ambiguity_is_error(tmp_path):
    resolver = SkillResolver([tmp_path])
    assert not resolver.lock("foo", SkillDependency(source="local", optional=True)).resolved
    make_skill(tmp_path / "a", "foo")
    make_skill(tmp_path / "b", "foo")
    with pytest.raises(ResolutionError, match="ambiguous"):
        SkillResolver([tmp_path / "a", tmp_path / "b"]).lock(
            "foo", SkillDependency(source="local", optional=True))


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *args],
                          env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def pinned_repo(tmp_path):
    skill = make_skill(tmp_path, "foo")
    git(tmp_path, "init")
    git(tmp_path, "add", "skills")
    git(tmp_path, "commit", "-m", "fixture")
    return tmp_path, skill, git(tmp_path, "rev-parse", "HEAD")


@pytest.mark.parametrize("ref", ["main", "v1.0.0", "HEAD", "abcd123", "0" * 40])
def test_ref_must_be_verified_full_local_head(pinned_repo, ref):
    root, _, _ = pinned_repo
    with pytest.raises(ResolutionError, match="ref"):
        SkillResolver([root]).lock("foo", SkillDependency(source="local", ref=ref))


def test_ref_without_git_evidence_is_rejected(tmp_path):
    make_skill(tmp_path, "foo")
    with pytest.raises(ResolutionError, match="ref"):
        SkillResolver([tmp_path]).lock("foo", SkillDependency(source="local", ref="a" * 40))


@pytest.mark.parametrize("drift", ["modified", "untracked", "ignored", "head", "index-flag", "missing-skip-worktree"])
def test_ref_lock_rechecks_live_git_state(pinned_repo, drift):
    root, skill, ref = pinned_repo
    resolver = SkillResolver([root])
    lock = resolver.lock("foo", SkillDependency(source="local", ref=ref, version="1.0.0"))
    assert resolver.verify_lock(lock) == (True, None)
    if drift == "modified":
        (skill / "VERSION").write_text("2.0.0")
    elif drift == "untracked":
        (root / "untracked.txt").write_text("dirty checkout")
    elif drift == "ignored":
        # An ignored file is still part of the package presented to the host.
        (root / ".git" / "info" / "exclude").write_text("hidden.txt\n")
        (skill / "hidden.txt").write_text("not in ref")
    elif drift == "index-flag":
        git(root, "update-index", "--assume-unchanged", "skills/foo/VERSION")
        (skill / "VERSION").write_text("2.0.0")
    elif drift == "missing-skip-worktree":
        git(root, "update-index", "--skip-worktree", "skills/foo/references/rules.md")
        (skill / "references" / "rules.md").unlink()
    else:
        git(root, "commit", "--allow-empty", "-m", "new head")
    assert resolver.verify_lock(lock)[0] is False
    with pytest.raises(ResolutionError):
        SkillResolver([root]).lock("foo", SkillDependency(source="local", ref=ref))


@pytest.mark.parametrize("filter_kind", ["clean", "process"])
def test_ref_verification_never_executes_repository_filters(pinned_repo, filter_kind):
    root, skill, ref = pinned_repo
    marker = root.parent / (root.name + "-filter-ran")
    (root / ".git" / "info" / "attributes").write_text("skills/foo/VERSION filter=probe\n")
    code = f"from pathlib import Path; import sys; Path({str(marker)!r}).touch(); sys.stdout.buffer.write(sys.stdin.buffer.read())"
    # The process fixture exits immediately: it need not implement Git's
    # handshake to prove that untrusted execution was attempted.
    if filter_kind == "process":
        code = f"from pathlib import Path; Path({str(marker)!r}).touch()"
    git(root, "config", f"filter.probe.{filter_kind}", shlex.join([sys.executable, "-c", code]))
    (skill / "VERSION").write_text("2.0.0\n")
    with pytest.raises(ResolutionError):
        SkillResolver([root]).lock("foo", SkillDependency(source="local", ref=ref))
    assert not marker.exists(), "ref validation executed a repository filter"
