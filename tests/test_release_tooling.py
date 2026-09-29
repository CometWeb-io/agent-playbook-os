"""Regression tests for the documented release-tooling environment."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _dev_dependencies() -> set[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    return set(project["project"]["optional-dependencies"]["dev"])


def test_dev_extra_contains_the_no_isolation_build_backend() -> None:
    dependencies = _dev_dependencies()

    assert any(item.startswith("setuptools>=") for item in dependencies)
    assert "wheel" in dependencies


@pytest.mark.skipif(
    os.environ.get("AGENT_PLAYBOOK_RUN_RELEASE_TOOLING") != "1",
    reason="nested venv release check is opt-in; set AGENT_PLAYBOOK_RUN_RELEASE_TOOLING=1",
)
def test_clean_dev_install_runs_reproducible_wheel_check(tmp_path: Path) -> None:
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True, clear=True)
    python = venv_dir / "bin" / "python"
    if sys.platform == "win32":
        python = venv_dir / "Scripts" / "python.exe"

    subprocess.run(
        [str(python), "-m", "pip", "install", "-e", ".[dev]"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [str(python), "-c", "import setuptools, wheel"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [str(python), "scripts/check_reproducible_wheel.py"],
        cwd=ROOT,
        check=True,
    )
