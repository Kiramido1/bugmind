"""Low-level durable file primitives: atomic writes, appends, advisory locks, backups."""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import time
from datetime import datetime, UTC
from pathlib import Path
from types import TracebackType

if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> None:  # locks byte 0 of the lock file; raises OSError when another process holds it
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no CRLF translation on raw writes


class LockTimeout(Exception):
    pass


class FileLock:
    """Exclusive lock (fcntl.flock / msvcrt.locking) on a lock file, with timeout.

    Safe across processes (two `bugmind` instances) and re-entrant within one
    FileLock object.
    """

    def __init__(self, path: Path, timeout: float = 10.0, poll: float = 0.05) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fd: int | None = None
        self._depth = 0

    def acquire(self) -> None:
        if self._fd is not None:
            self._depth += 1
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _try_lock(fd)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):  # EDEADLK: msvcrt busy
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockTimeout(f"Timed out waiting for lock {self.path}") from exc
                time.sleep(self.poll)
        self._fd = fd
        self._depth = 1

    def release(self) -> None:
        if self._fd is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None) -> None:
        self.release()


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace(src: str, dst: Path) -> None:
    """os.replace, retried on Windows where a reader holding *dst* open makes it fail briefly."""
    for attempt in range(50):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            time.sleep(0.02)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write *text* to *path* atomically (temp file + fsync + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        _replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def append_line_durable(path: Path, line: str) -> None:
    """Append one line (newline added) and fsync. Caller must hold the store lock.

    If the file does not end with a newline (e.g. a previous crash mid-write left a
    partial record), a newline is inserted first so the partial line stays isolated
    and is reported as corrupt rather than merged with the new record.
    """
    if "\n" in line:
        raise ValueError("JSONL line must not contain newlines")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                prefix = "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _BINARY, 0o600)
    try:
        os.write(fd, (prefix + line + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_file(path: Path, backup_dir: Path, keep: int = 20) -> Path | None:
    """Copy *path* into *backup_dir* with a timestamp; prune to the newest *keep* copies."""
    path = Path(path)
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, target)
    olds = sorted(backup_dir.glob(f"{path.stem}.*{path.suffix}"))
    for old in olds[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return target
