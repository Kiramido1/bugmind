"""Canonical bug archive: an append-only JSONL log of bug revisions.

Each line is a complete `BugRecord` revision. The latest revision of an ID is
the current state; earlier revisions are kept forever, so historical evidence
and the original user description can never be overwritten. Corrupted lines
are reported, never silently dropped or rewritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from pydantic import ValidationError

from bugmind.models import BUG_ID_RE, BugRecord, utcnow
from bugmind.storage.atomic import FileLock, append_line_durable, atomic_write_text


@dataclass(frozen=True)
class StoreLayout:
    root: Path

    @property
    def bugs_file(self) -> Path:
        return self.root / "bugs" / "bugs.jsonl"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def attachments_dir(self) -> Path:
        return self.root / "attachments"

    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    @property
    def backups_dir(self) -> Path:
        return self.root / "memory" / ".backups"

    @property
    def lock_file(self) -> Path:
        return self.root / ".bugmind.lock"

    def ensure(self) -> None:
        for d in (self.bugs_file.parent, self.reports_dir, self.memory_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.bugs_file.exists():
            self.bugs_file.touch(mode=0o600)


@dataclass
class CorruptRecord:
    line_no: int
    error: str
    excerpt: str


@dataclass
class _Snapshot:
    key: tuple[int, int]
    revisions: dict[str, list[BugRecord]] = field(default_factory=dict)
    corrupt: list[CorruptRecord] = field(default_factory=list)


class BugStore:
    def __init__(self, root: Path, lock_timeout: float = 10.0) -> None:
        self.layout = StoreLayout(Path(root))
        self.layout.ensure()
        self.lock = FileLock(self.layout.lock_file, timeout=lock_timeout)
        self._snap: _Snapshot | None = None

    # ------------------------------------------------------------------ read
    def _read(self) -> _Snapshot:
        path = self.layout.bugs_file
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            key = (0, 0)
        if self._snap and self._snap.key == key:
            return self._snap
        snap = _Snapshot(key=key)
        if key != (0, 0):
            with open(path, encoding="utf-8", errors="replace") as fh:
                for no, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = BugRecord.model_validate(json.loads(line))
                    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
                        snap.corrupt.append(CorruptRecord(no, str(exc).splitlines()[0][:200], line[:120]))
                        continue
                    snap.revisions.setdefault(rec.id, []).append(rec)
        for revs in snap.revisions.values():
            revs.sort(key=lambda r: r.revision)
        self._snap = snap
        return snap

    def corrupt_records(self) -> list[CorruptRecord]:
        return list(self._read().corrupt)

    def all(self) -> list[BugRecord]:
        """Latest revision of every bug, ordered by numeric id."""
        snap = self._read()
        latest = [revs[-1] for revs in snap.revisions.values()]
        return sorted(latest, key=lambda r: _num(r.id))

    def get(self, bug_id: str) -> BugRecord | None:
        revs = self._read().revisions.get(bug_id.upper())
        return revs[-1] if revs else None

    def history(self, bug_id: str) -> list[BugRecord]:
        return list(self._read().revisions.get(bug_id.upper(), []))

    def search(self, query: str) -> list[BugRecord]:
        terms = [t for t in query.lower().split() if t]
        return [r for r in self.all() if all(t in r.search_blob() for t in terms)]

    def __len__(self) -> int:
        return len(self._read().revisions)

    # ----------------------------------------------------------------- write
    def next_id(self) -> str:
        nums = [_num(i) for i in self._read().revisions]
        # Also consider IDs on corrupt lines so a damaged record's ID is never reused.
        for c in self._read().corrupt:
            for token in c.excerpt.replace('"', " ").split():
                if BUG_ID_RE.match(token):
                    nums.append(_num(token))
        return f"BUG-{(max(nums) + 1 if nums else 1):04d}"

    def create(self, build: Callable[[str], BugRecord]) -> BugRecord:
        """Allocate the next ID and persist the record built by *build(bug_id)* atomically."""
        with self.lock:
            self._snap = None
            bug_id = self.next_id()
            record = build(bug_id)
            if record.id != bug_id:
                raise ValueError("builder must use the allocated id")
            record = record.model_copy(update={"revision": 1})
            BugRecord.model_validate(record.model_dump())  # re-validate before writing
            append_line_durable(self.layout.bugs_file, record.model_dump_json())
            self._snap = None
            return record

    def update(self, bug_id: str, mutate: Callable[[BugRecord], BugRecord]) -> BugRecord:
        """Append a new revision. Original input, creation time and evidence are immutable."""
        with self.lock:
            self._snap = None
            current = self.get(bug_id)
            if current is None:
                raise KeyError(bug_id)
            new = mutate(current.model_copy(deep=True))
            evidence = list(current.analysis.evidence)
            evidence += [e for e in new.analysis.evidence if e not in evidence]
            analysis = new.analysis.model_copy(update={"evidence": evidence})
            new = new.model_copy(update={
                "id": current.id,
                "revision": current.revision + 1,
                "original_input": current.original_input,
                "created_at": current.created_at,
                "updated_at": utcnow(),
                "analysis": analysis,
            })
            BugRecord.model_validate(new.model_dump())
            append_line_durable(self.layout.bugs_file, new.model_dump_json())
            self._snap = None
            return new

    def write_report(self, record: BugRecord) -> Path:
        path = self.layout.reports_dir / f"{record.id}.md"
        atomic_write_text(path, record.formatted_report.rstrip() + "\n")
        return path


def _num(bug_id: str) -> int:
    m = BUG_ID_RE.match(bug_id)
    return int(m.group(1)) if m else 0
