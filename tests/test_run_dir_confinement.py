"""CLI --run-id must stay a single segment under --run-root."""

from pathlib import Path

import pytest

from agent_playbook_os.cli import _run_dir


def test_run_dir_accepts_uuid_like_id(tmp_path):
    run_root = tmp_path / "runs"
    run_root.mkdir()
    run_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    run_dir = _run_dir(run_root, run_id)
    assert run_dir.parent == run_root.resolve()
    assert run_dir.name == run_id
    run_dir.mkdir()
    marker = run_dir / "state.json"
    marker.write_text("{}", encoding="utf-8")
    assert marker.exists()
    assert marker.is_relative_to(run_root.resolve())


def test_run_dir_rejects_parent_traversal(tmp_path):
    run_root = tmp_path / "runs"
    run_root.mkdir()
    outside_before = {p.name for p in tmp_path.iterdir()}
    with pytest.raises(SystemExit, match="single path segment"):
        _run_dir(run_root, "../escape")
    assert {p.name for p in tmp_path.iterdir()} == outside_before
    assert not (tmp_path / "escape").exists()
    assert list(run_root.iterdir()) == []


def test_run_dir_rejects_absolute_id(tmp_path):
    run_root = tmp_path / "runs"
    run_root.mkdir()
    abs_id = str(tmp_path / "outside-abs")
    with pytest.raises(SystemExit, match="single path segment"):
        _run_dir(run_root, abs_id)
    assert not Path(abs_id).exists()
    assert list(run_root.iterdir()) == []


@pytest.mark.parametrize(
    "bad_id",
    ["", ".", "..", "a/b", r"a\b", "/tmp/x", "nested/../x"],
)
def test_run_dir_rejects_non_single_segments(tmp_path, bad_id):
    with pytest.raises(SystemExit, match="invalid --run-id"):
        _run_dir(tmp_path / "runs", bad_id)
