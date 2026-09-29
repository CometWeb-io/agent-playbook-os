import importlib.util
from pathlib import Path

import pytest


def release_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
    spec = importlib.util.spec_from_file_location("release_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("directory", [".superpowers", ".venv", ".git", "build"])
def test_release_excludes_private_workspace_before_traversal(tmp_path, monkeypatch, directory):
    builder = release_module()
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    (tmp_path / "README.md").write_text("public")
    private = tmp_path / directory
    private.mkdir()
    (private / "private.txt").write_text("not for distribution")
    (private / "python").symlink_to("/nonexistent-test-interpreter")
    assert [p.relative_to(tmp_path).as_posix() for p in builder.included_files()] == ["README.md"]


def test_release_still_rejects_symlink_in_public_tree(tmp_path, monkeypatch):
    builder = release_module()
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    (tmp_path / "public.py").symlink_to("/nonexistent-test-source")
    with pytest.raises(RuntimeError, match="symlink"):
        list(builder.included_files())
