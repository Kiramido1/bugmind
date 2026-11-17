import json
import multiprocessing as mp
import os
import sys
import time

import pytest

from bugmind.models import BugAnalysis, BugRecord, ConfirmationStatus
from bugmind.storage.atomic import FileLock, LockTimeout, append_line_durable, atomic_write_text, backup_file
from bugmind.storage.bug_store import BugStore


def make(bug_id: str, text: str = "x", **kw) -> BugRecord:
    return BugRecord(id=bug_id, original_input=text, analysis=BugAnalysis(title=f"T {text}", evidence=["e1"]), **kw)


def test_ids_are_sequential_and_persist_across_reopen(tmp_path):
    store = BugStore(tmp_path)
    a = store.create(lambda i: make(i, "one"))
    b = store.create(lambda i: make(i, "two"))
    assert (a.id, b.id) == ("BUG-0001", "BUG-0002")
    reopened = BugStore(tmp_path)
    assert [r.id for r in reopened.all()] == ["BUG-0001", "BUG-0002"]
    assert reopened.get("bug-0002").original_input == "two"
    assert reopened.next_id() == "BUG-0003"


def test_id_grows_past_four_digits(tmp_path):
    store = BugStore(tmp_path)
    append_line_durable(store.layout.bugs_file, make("BUG-9999").model_dump_json())
    assert store.next_id() == "BUG-10000"


def test_update_appends_revision_and_never_overwrites_evidence(tmp_path):
    store = BugStore(tmp_path)
    store.create(lambda i: make(i, "original description"))

    def mutate(r: BugRecord) -> BugRecord:
        a = r.analysis.model_copy(update={"evidence": ["e2"], "title": "new title"})
        return r.model_copy(update={"original_input": "HACKED", "analysis": a, "status": ConfirmationStatus.CONFIRMED})

    new = store.update("BUG-0001", mutate)
    assert new.revision == 2
    assert new.original_input == "original description"  # immutable
    assert new.analysis.evidence == ["e1", "e2"]  # union, never dropped
    assert new.status == ConfirmationStatus.CONFIRMED
    hist = store.history("BUG-0001")
    assert [h.revision for h in hist] == [1, 2] and hist[0].analysis.title == "T original description"
    lines = store.layout.bugs_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # append-only


def test_corrupt_lines_are_reported_not_fatal_and_ids_not_reused(tmp_path):
    store = BugStore(tmp_path)
    store.create(lambda i: make(i, "good"))
    with open(store.layout.bugs_file, "a", encoding="utf-8", newline="") as fh:
        fh.write('{"id": "BUG-0002", "original_input": "trunc')  # crash mid-write, no newline
    store = BugStore(tmp_path)
    assert [r.id for r in store.all()] == ["BUG-0001"]
    assert len(store.corrupt_records()) == 1
    rec = store.create(lambda i: make(i, "after crash"))
    assert rec.id == "BUG-0003"  # BUG-0002 seen on the corrupt line is never reused
    assert store.get("BUG-0003").original_input == "after crash"  # new line not merged with partial one


def test_invalid_record_fails_validation_before_write(tmp_path):
    store = BugStore(tmp_path)
    with pytest.raises(ValueError):
        store.create(lambda i: make("BUG-0042"))
    assert store.layout.bugs_file.read_text(encoding="utf-8") == ""


def test_atomic_write_replaces_and_leaves_no_temp_files(tmp_path):
    target = tmp_path / "f.md"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["f.md"]
    if os.name != "nt":  # Windows has no POSIX permission bits
        assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_atomic_write_failure_keeps_old_content(tmp_path, monkeypatch):
    target = tmp_path / "f.md"
    atomic_write_text(target, "safe")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "partial")
    assert target.read_text(encoding="utf-8") == "safe"
    assert [p.name for p in tmp_path.iterdir()] == ["f.md"]


def test_backup_rotation(tmp_path):
    f = tmp_path / "M.md"
    for n in range(5):
        f.write_text(str(n))
        backup_file(f, tmp_path / "bk", keep=3)
        time.sleep(0.001)
    backups = sorted((tmp_path / "bk").iterdir())
    assert len(backups) == 3 and backups[-1].read_text(encoding="utf-8") == "4"


def _hold_lock(path, ready, release):
    with FileLock(path):
        ready.set()
        release.wait(5)


def test_file_lock_excludes_other_processes(tmp_path):
    ctx = mp.get_context("spawn" if sys.platform == "win32" else "fork")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(tmp_path / "l", ready, release))
    p.start()
    assert ready.wait(30)  # Windows spawns a fresh interpreter
    with pytest.raises(LockTimeout):
        FileLock(tmp_path / "l", timeout=0.3).acquire()
    release.set()
    p.join(5)
    with FileLock(tmp_path / "l", timeout=1):
        pass


def test_file_lock_is_reentrant(tmp_path):
    lock = FileLock(tmp_path / "l")
    with lock:
        with lock:
            pass
    assert lock._fd is None


def _create_many(root, n):
    store = BugStore(root)
    for k in range(n):
        store.create(lambda i, k=k: make(i, f"{os.getpid()}-{k}"))


def test_concurrent_processes_get_unique_ids(tmp_path):
    ctx = mp.get_context("spawn" if sys.platform == "win32" else "fork")
    procs = [ctx.Process(target=_create_many, args=(tmp_path, 10)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    store = BugStore(tmp_path)
    ids = [r.id for r in store.all()]
    assert len(ids) == 40 and len(set(ids)) == 40
    assert not store.corrupt_records()
    for line in store.layout.bugs_file.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_escape_markup_keeps_untrusted_text_literal():
    from textual.content import Content

    from bugmind.security import escape_markup
    for text in ["[Customers API] x", "[API / Orders] [/] broken [b", "[$primary]x", "ends with \\", "a\\[b]c"]:
        assert Content.from_markup(f"[b]{escape_markup(text)}[/]").plain.rstrip(" ") == text
