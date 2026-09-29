"""Real, bounded subprocess regressions for the command trust boundary."""
import asyncio
import os
from pathlib import Path
import shutil
import sys

import pytest

from agent_playbook_os.errors import ExternalExecutionRequired, ReconciliationRequired
from agent_playbook_os.models import StepSpec, Playbook, InvocationStatus
from agent_playbook_os.compiler import Compiler
from agent_playbook_os.engine import Runner
from agent_playbook_os.runtime import ReferenceRuntime


STEP = StepSpec(id="command", type="action", action="command", side_effects="local")


def runtime(**options):
    return ReferenceRuntime(allow_commands=True, allowed_commands={sys.executable}, **options)


async def wait_for_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="executable script fixture uses POSIX shebang")
async def test_same_basename_at_unapproved_path_is_rejected(tmp_path):
    fake = tmp_path / "echo"
    fake.write_text("#!/bin/sh\nprintf forged\n")
    fake.chmod(0o700)
    allowed = ReferenceRuntime(allow_commands=True, allowed_commands={"echo"})
    with pytest.raises(ExternalExecutionRequired, match="allowlist"):
        await allowed.execute_action(STEP, {"argv": [str(fake)]}, {})


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="executable script fixture uses POSIX shebang")
async def test_bare_command_is_pinned_before_path_changes(tmp_path, monkeypatch):
    original = shutil.which("echo")
    assert original
    allowed = ReferenceRuntime(allow_commands=True, allowed_commands={"echo"})
    fake = tmp_path / "echo"
    fake.write_text("#!/bin/sh\nprintf forged\n")
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = await allowed.execute_action(STEP, {"argv": ["echo", "expected"]}, {})
    assert result["stdout"].strip() == "expected"


@pytest.mark.asyncio
async def test_cancellation_reaps_command_before_returning(tmp_path):
    ready, late = tmp_path / "ready", tmp_path / "late"
    code = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(.3); Path({str(late)!r}).touch()"
    task = asyncio.create_task(runtime().execute_action(STEP, {"argv": [sys.executable, "-c", code]}, {}))
    try:
        await wait_for_file(ready)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.4)
        assert not late.exists(), "child continued after the cancelled action returned"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_runner_task_also_cancels_its_active_command(tmp_path):
    ready, late = tmp_path / "ready", tmp_path / "late"
    code = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(.3); Path({str(late)!r}).touch()"
    playbook = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "cancel", "version": "0.1.0", "description": "runner cancellation"},
        "spec": {"steps": [{"id": "cmd", "type": "action", "action": "command", "with": {"argv": [sys.executable, "-c", code]}}]},
    })
    task = asyncio.create_task(Runner(runtime()).run(Compiler().compile(playbook), tmp_path / "run"))
    try:
        await wait_for_file(ready)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.4)
        assert not late.exists()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group lifecycle is POSIX-specific")
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_command_cleanup_stops_descendants(tmp_path, stop):
    ready, late = tmp_path / "ready", tmp_path / "late"
    child = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(.6); Path({str(late)!r}).touch()"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(1)"
    task = asyncio.create_task(runtime().execute_action(STEP, {"argv": [sys.executable, "-c", parent], "timeout": 0.3 if stop == "timeout" else 3}, {}))
    try:
        await wait_for_file(ready)
        if stop == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if stop == "cancel" else TimeoutError):
            await task
        await asyncio.sleep(0.7)
        assert not late.exists(), "descendant outlived the stopped command"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_output_limit_stops_process_during_execution(tmp_path):
    late = tmp_path / "late"
    code = f"import os, time; from pathlib import Path; os.write(1, b'x'*4096); time.sleep(.4); Path({str(late)!r}).touch()"
    from agent_playbook_os.errors import SideEffectOutcomeUnknown
    with pytest.raises(SideEffectOutcomeUnknown, match="max_command_output_bytes"):
        await runtime(max_command_output_bytes=32).execute_action(STEP, {"argv": [sys.executable, "-c", code]}, {})
    assert not late.exists(), "limit was checked only after process completion"


@pytest.mark.asyncio
async def test_output_limit_is_combined_and_exact_boundary_is_allowed():
    allowed = runtime(max_command_output_bytes=8)
    value = await allowed.execute_action(STEP, {"argv": [sys.executable, "-c", "import os; os.write(1,b'1234'); os.write(2,b'abcd')"]}, {})
    assert (value["stdout"], value["stderr"], value["returncode"]) == ("1234", "abcd", 0)
    with pytest.raises(ValueError, match="max_command_output_bytes"):
        await allowed.execute_action(STEP.model_copy(update={"side_effects": "none"}), {"argv": [sys.executable, "-c", "import os; os.write(1,b'12345'); os.write(2,b'abcd')"]}, {})


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_output_limit_is_rejected(limit):
    with pytest.raises(ValueError, match="max_command_output_bytes"):
        runtime(max_command_output_bytes=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
async def test_nonfinite_or_nonpositive_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="timeout"):
        await runtime().execute_action(STEP, {"argv": [sys.executable, "-c", "pass"], "timeout": timeout}, {})


@pytest.mark.asyncio
async def test_output_overflow_after_local_dispatch_requires_reconciliation(tmp_path):
    marker = tmp_path / "writes.txt"
    code = f"from pathlib import Path; import os,time; f=Path({str(marker)!r}).open('a'); f.write('write\\n'); f.close(); os.write(1,b'x'*4096); time.sleep(3)"
    playbook = Playbook.model_validate({
        "apiVersion": "playbook.agent/v1alpha1", "kind": "Playbook",
        "metadata": {"id": "overflow", "version": "0.1.0", "description": "Overflow recovery"},
        "spec": {"steps": [{"id": "write", "type": "action", "action": "command",
                            "side_effects": "local", "retry": {"max_attempts": 2},
                            "with": {"argv": [sys.executable, "-c", code]}}]},
    })
    plan = Compiler().compile(playbook)
    runner = Runner(runtime(max_command_output_bytes=32))
    run_dir = tmp_path / "run"
    state = await runner.run(plan, run_dir)
    assert marker.read_text().splitlines() == ["write"]
    assert state.invocations[-1].status == InvocationStatus.UNKNOWN
    with pytest.raises(ReconciliationRequired):
        await runner.run(plan, run_dir, resume=True)
    assert marker.read_text().splitlines() == ["write"]
