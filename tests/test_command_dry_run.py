"""Previewing a permitted command must never execute that command."""
from pathlib import Path
import sys

import pytest

from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.errors import ExternalExecutionRequired
from agent_playbook_os.models import Playbook, RunStatus, StepSpec
from agent_playbook_os.runtime import ReferenceRuntime


STEP = StepSpec(id="cmd", type="action", action="command", side_effects="local")


def preview_runtime(tmp_path):
    return ReferenceRuntime(
        dry_run=True, allow_commands=True, allowed_commands={sys.executable},
        command_cwd=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_preview_of_write_command_does_not_create_file(tmp_path):
    marker = tmp_path / "must-not-exist"
    argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    result = await preview_runtime(tmp_path).execute_action(STEP, {"argv": argv}, {})
    assert not marker.exists(), "dry-run executed a real filesystem write"
    assert result == {
        "kind": "command-invocation", "dry_run": True, "argv": argv,
        "executable": str(Path(sys.executable).resolve()),
        "cwd": str(tmp_path.resolve()), "timeout": 30.0,
    }
    # A preview does not invent stdout, an exit code, or successful execution.
    assert "returncode" not in result


@pytest.mark.asyncio
async def test_strict_runner_preview_does_not_execute_command(tmp_path):
    marker = tmp_path / "must-not-exist"
    argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    playbook = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "preview", "version": "0.1.0", "description": "command preview"},
        "spec": {"steps": [{"id": "cmd", "type": "action", "action": "command", "with": {"argv": argv}}]},
    })
    state = await Runner(preview_runtime(tmp_path), enforce_capabilities=True).run(
        Compiler().compile(playbook), tmp_path / "run",
    )
    assert state.status == RunStatus.COMPLETED
    assert not marker.exists(), "runner dry-run dispatched the command"
    assert state.steps["cmd"].output["dry_run"] is True


@pytest.mark.asyncio
async def test_preview_still_rejects_disabled_commands():
    with pytest.raises(ExternalExecutionRequired, match="disabled"):
        await ReferenceRuntime(dry_run=True).execute_action(STEP, {"argv": [sys.executable]}, {})


@pytest.mark.asyncio
async def test_preview_still_rejects_unapproved_executable(tmp_path):
    with pytest.raises(ExternalExecutionRequired, match="allowlist"):
        await preview_runtime(tmp_path).execute_action(STEP, {"argv": [str(tmp_path / "unapproved")]}, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("inputs", [{"argv": "echo unsafe"}, {"argv": []},
    {"argv": [sys.executable], "timeout": float("nan")},
    {"argv": [sys.executable], "timeout": 0}])
async def test_preview_still_validates_inputs(tmp_path, inputs):
    with pytest.raises(ValueError):
        await preview_runtime(tmp_path).execute_action(STEP, inputs, {})
