"""Local command identity, bounded output and owned-process cleanup.

This is not an OS sandbox: allowlisted programs and their arguments remain
trusted code. POSIX children share an owned session for group cleanup.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import signal

from .errors import ExternalExecutionRequired


class CommandOutputLimitExceeded(ValueError):
    """Output collection interrupted a command that was already dispatched."""


class CommandAllowlist:
    def __init__(self, entries: set[str], cwd: str | None):
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.aliases: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, str) or not entry:
                raise ValueError("command allowlist entries must be non-empty strings")
            bare = Path(entry).name == entry
            located = shutil.which(entry) if bare else entry
            if not located:
                raise ExternalExecutionRequired(f"command allowlist executable unavailable: {entry}")
            path = Path(located)
            if not path.is_absolute():
                path = self.cwd / path
            try:
                path = path.resolve(strict=True)
            except OSError as exc:
                raise ExternalExecutionRequired(f"command allowlist executable unavailable: {entry}") from exc
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ExternalExecutionRequired(f"command allowlist executable unavailable: {entry}")
            self.aliases[entry] = str(path)
        self.paths = frozenset(self.aliases.values())

    def resolve(self, requested: str) -> str:
        # Bare names are resolved once, not again through a possibly changed PATH.
        if requested in self.aliases and Path(requested).name == requested:
            return self.aliases[requested]
        path = Path(requested)
        if not path.is_absolute():
            path = self.cwd / path
        try:
            resolved = str(path.resolve(strict=True))
        except OSError:
            resolved = ""
        if resolved not in self.paths:
            raise ExternalExecutionRequired(f"command executable not in allowlist: {requested}")
        return resolved


async def run_command(argv: list[str], *, executable: str, cwd: str, timeout: float, max_output: int) -> dict:
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, *argv[1:], cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix", limit=min(65536, max_output + 1),
    ))
    readers: list[asyncio.Task] = []
    total = 0

    async def read_bounded(stream):
        nonlocal total
        output = bytearray()
        while chunk := await stream.read(min(65536, max_output + 1)):
            total += len(chunk)
            if total > max_output:
                raise CommandOutputLimitExceeded(f"command output exceeded max_command_output_bytes={max_output}")
            output.extend(chunk)
        return output.decode(errors="replace")

    async def cleanup():
        # Shield spawn so cancellation cannot lose ownership of a created child.
        try:
            proc = await spawn
        except Exception:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

        async def discard(stream):
            while await stream.read(65536):
                pass

        # Drain without retaining bytes, including when a reader hit the cap.
        await asyncio.gather(discard(proc.stdout), discard(proc.stderr))
        await proc.wait()

    try:
        async with asyncio.timeout(timeout):
            proc = await asyncio.shield(spawn)
            readers = [asyncio.create_task(read_bounded(proc.stdout)), asyncio.create_task(read_bounded(proc.stderr))]
            stdout, stderr = await asyncio.gather(*readers)
            returncode = await proc.wait()
            return {"argv": argv, "returncode": returncode, "stdout": stdout, "stderr": stderr}
    except TimeoutError as exc:
        raise TimeoutError(f"command timed out after {timeout}s") from exc
    finally:
        cleanup_task = asyncio.create_task(cleanup())
        cancelled_during_cleanup = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled_during_cleanup = True
        cleanup_task.result()
        if cancelled_during_cleanup:
            raise asyncio.CancelledError
