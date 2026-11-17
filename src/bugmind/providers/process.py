"""Safe asyncio subprocess execution: argv only (never a shell), timeouts, process-tree kill.

POSIX: the agent gets its own session and the whole process group is signalled.
Windows: the agent is put in a Job Object, and terminating the job kills every descendant.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from bugmind.providers.base import ErrorKind, ProviderError

STDERR_CAP = 64_000
LINE_LIMIT = 32 * 1024 * 1024
_BATCH_UNSAFE = set('"%!^&|<>\r\n')  # cmd.exe re-parses a .cmd/.bat's arguments (BatBadBut)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    def _attach_job(proc: asyncio.subprocess.Process) -> None:
        """Put *proc* in a kill-on-close Job Object so its children can be killed with it.

        shortcut: assigned right after spawn, so a child started in the first microseconds escapes the job;
        upgrade to CREATE_SUSPENDED + resume if an agent ever forks that early.
        """
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        handle = _k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        ok = bool(handle) and _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) \
            and _k32.AssignProcessToJobObject(job, handle)
        if handle:
            _k32.CloseHandle(handle)
        if ok:
            proc._bugmind_job = job  # type: ignore[attr-defined]
        else:
            _k32.CloseHandle(job)


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
    if os.sep in expanded or (os.altsep and os.altsep in expanded):
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        raise ProviderError(ErrorKind.MISSING, f"Executable not found or not executable: {expanded}", provider)
    found = shutil.which(expanded)
    if not found:
        raise ProviderError(
            ErrorKind.MISSING, f"'{expanded}' was not found on PATH. Install it or set its path in config.", provider
        )
    return found


def _kill_group(proc: asyncio.subprocess.Process, force: bool) -> None:
    if sys.platform == "win32":  # no graceful signal for console agents: terminate the whole job
        job = getattr(proc, "_bugmind_job", None)
        if job:
            _k32.TerminateJobObject(job, 1)
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass


def _release(proc: asyncio.subprocess.Process) -> None:
    """Final sweep: kill anything the agent left running in the background (and free the Windows job)."""
    _kill_group(proc, force=True)
    if sys.platform == "win32" and (job := getattr(proc, "_bugmind_job", None)):
        _k32.CloseHandle(job)
        proc._bugmind_job = None  # type: ignore[attr-defined]


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    if proc.returncode is None:
        _kill_group(proc, force=False)
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except TimeoutError:
            pass
    # Always sweep the group: the leader may be gone while a background child still runs.
    _release(proc)
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
    if sys.platform == "win32" and argv[0].lower().endswith((".cmd", ".bat")):
        if bad := next((a for a in argv[1:] if _BATCH_UNSAFE & set(a)), None):
            raise ProviderError(
                ErrorKind.PROCESS,
                f"Refusing to pass {bad!r} to the batch file {argv[0]}: cmd.exe would interpret its special "
                "characters. Rename the file or point the provider at the agent's .exe.", provider)
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
            cwd=str(cwd) if cwd else None,
            limit=LINE_LIMIT,
            **({"creationflags": 0x00000200} if sys.platform == "win32"  # CREATE_NEW_PROCESS_GROUP
               else {"start_new_session": True}),  # own process group so we can kill children too
        )
    except FileNotFoundError as exc:
        raise ProviderError(ErrorKind.MISSING, f"Executable not found: {argv[0]}", provider) from exc
    except PermissionError as exc:
        raise ProviderError(ErrorKind.MISSING, f"Permission denied executing {argv[0]}", provider) from exc
    except OSError as exc:  # e.g. E2BIG: argument list too long
        raise ProviderError(ErrorKind.PROCESS, f"Could not start {argv[0]}: {exc.strerror or exc}", provider) from exc
    if sys.platform == "win32":
        _attach_job(proc)

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
                on_line(line.rstrip("\r\n"))

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
    _release(proc)
    return ProcessOutput(returncode, "".join(stdout_parts), "".join(stderr_parts), time.monotonic() - started)


async def probe_version(executable: str, provider: str, args: tuple[str, ...] = ("--version",)) -> str:
    exe = resolve_executable(executable, provider)
    out = await run_process([exe, *args], provider=provider, stdin_data=None, timeout=20)
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0][:80] if text else "unknown"
