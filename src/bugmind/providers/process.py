"""Safe asyncio subprocess execution: argv only (never a shell), timeouts, group kill."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from bugmind.providers.base import ErrorKind, ProviderError

STDERR_CAP = 64_000
LINE_LIMIT = 32 * 1024 * 1024


@dataclass
class ProcessOutput:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float


def resolve_executable(executable: str, provider: str) -> str:
    """Resolve an executable name/path without invoking a shell."""
    if not executable:
        raise ProviderError(ErrorKind.MISSING, "No executable configured", provider)
    expanded = os.path.expanduser(executable)
    if os.sep in expanded:
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        raise ProviderError(ErrorKind.MISSING, f"Executable not found or not executable: {expanded}", provider)
    found = shutil.which(expanded)
    if not found:
        raise ProviderError(
            ErrorKind.MISSING, f"'{expanded}' was not found on PATH. Install it or set its path in config.", provider
        )
    return found


def _kill_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    if proc.returncode is None:
        _kill_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except TimeoutError:
            pass
    # Always sweep the group: the leader may be gone while a background child still runs.
    _kill_group(proc, signal.SIGKILL)
    await proc.wait()


async def run_process(
    argv: list[str],
    *,
    provider: str,
    stdin_data: str | None,
    timeout: float,
    on_line: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> ProcessOutput:
    """Run *argv* (list, no shell), feed stdin, stream stdout lines to *on_line*."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
            cwd=str(cwd) if cwd else None,
            start_new_session=True,  # own process group so we can kill children too
            limit=LINE_LIMIT,
        )
    except FileNotFoundError as exc:
        raise ProviderError(ErrorKind.MISSING, f"Executable not found: {argv[0]}", provider) from exc
    except PermissionError as exc:
        raise ProviderError(ErrorKind.MISSING, f"Permission denied executing {argv[0]}", provider) from exc
    except OSError as exc:  # e.g. E2BIG: argument list too long
        raise ProviderError(ErrorKind.PROCESS, f"Could not start {argv[0]}: {exc.strerror or exc}", provider) from exc

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    async def feed() -> None:
        if stdin_data is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(stdin_data.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def read_stdout() -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            stdout_parts.append(line)
            if on_line:
                on_line(line.rstrip("\n"))

    async def read_stderr() -> None:
        assert proc.stderr is not None
        size = 0
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            if size < STDERR_CAP:
                stderr_parts.append(chunk.decode("utf-8", errors="replace"))
                size += len(chunk)

    async def everything() -> int:
        await asyncio.gather(feed(), read_stdout(), read_stderr())
        return await proc.wait()

    try:
        returncode = await asyncio.wait_for(everything(), timeout)
    except TimeoutError as exc:
        await _terminate(proc)
        raise ProviderError(ErrorKind.TIMEOUT, f"No result within {timeout:.0f}s; process terminated", provider) from exc
    except BaseException as exc:  # cancel, oversized line, failing on_line parser… never leave the agent running
        await asyncio.shield(_terminate(proc))
        if isinstance(exc, (ProviderError, asyncio.CancelledError)) or not isinstance(exc, Exception):
            raise
        raise ProviderError(ErrorKind.PROTOCOL, f"Could not read agent output: {exc}", provider) from exc
    _kill_group(proc, signal.SIGKILL)  # reap anything the agent left running in the background
    return ProcessOutput(returncode, "".join(stdout_parts), "".join(stderr_parts), time.monotonic() - started)


async def probe_version(executable: str, provider: str, args: tuple[str, ...] = ("--version",)) -> str:
    exe = resolve_executable(executable, provider)
    out = await run_process([exe, *args], provider=provider, stdin_data=None, timeout=20)
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0][:80] if text else "unknown"
