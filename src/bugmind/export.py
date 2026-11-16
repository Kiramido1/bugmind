"""On-demand knowledge export (manual only; no scheduling)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, UTC
from pathlib import Path

from bugmind import __version__
from bugmind.service import BugMindService
from bugmind.storage.atomic import atomic_write_text

TARGETS = {
    "prevention": "BACKEND_PREVENTION.md",
    "patterns": "BUG_PATTERNS.md",
    "stats": "STATISTICS.md",
}


class ExportError(Exception):
    pass


def downloads_dir() -> Path:
    """The user's Downloads folder: XDG_DOWNLOAD_DIR from user-dirs.dirs (localized names), else ~/Downloads."""
    import re
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        m = re.search(r'^XDG_DOWNLOAD_DIR="([^"]+)"', conf.read_text(encoding="utf-8"), re.M)
    except OSError:
        m = None
    if m:  # the file only ever uses $HOME expansion; never expand anything else from it
        path = Path(m.group(1).replace("$HOME", str(Path.home()), 1))
        if path.is_absolute() and path != Path.home():
            return path
    return Path.home() / "Downloads"


def _unique(dest: Path) -> Path:
    """Browser-style: 'NAME.md', then 'NAME (1).md', 'NAME (2).md'…"""
    n = 1
    candidate = dest
    while candidate.exists():
        candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        n += 1
    return candidate


def export_knowledge(svc: BugMindService, what: str, output: Path, *, as_zip: bool = False,
                     force: bool = False, unique: bool = False) -> Path:
    """Export one memory file or the full package ('all'). Returns the created path.

    unique=True never overwrites: an existing file gets a ' (n)' suffix (used for Downloads)."""
    svc.rebuild_memory()  # always export the latest validated state
    layout = svc.store.layout
    output = output.expanduser()
    if what in TARGETS:
        name = TARGETS[what]
        if unique:
            output.mkdir(parents=True, exist_ok=True)
        dest = output / name if output.is_dir() else output
        if unique:
            dest = _unique(dest)
        elif dest.exists() and not force:
            raise ExportError(f"{dest} already exists (use --force to overwrite)")
        atomic_write_text(dest, (layout.memory_dir / name).read_text(encoding="utf-8"))
        return dest
    if what != "all":
        raise ExportError(f"Unknown export target {what!r}; choose prevention, patterns, stats or all")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = f"bugmind-knowledge-{svc.cfg.storage.project}-{stamp}"
    final = output / (base + ".zip" if as_zip else base)
    if final.exists() and not force:
        raise ExportError(f"{final} already exists (use --force to overwrite)")
    output.mkdir(parents=True, exist_ok=True)
    # Build in a private staging dir: a --zip export must never touch an existing package directory.
    staging = Path(tempfile.mkdtemp(prefix=".bugmind-export-", dir=output))
    try:
        pkg = staging / base
        pkg.mkdir()
        with svc.store.lock:  # consistent snapshot: no half-written bug while we copy
            for name in TARGETS.values():
                shutil.copy2(layout.memory_dir / name, pkg / name)
            shutil.copytree(layout.reports_dir, pkg / "reports")
            if layout.attachments_dir.exists():
                shutil.copytree(layout.attachments_dir, pkg / "attachments")
            shutil.copy2(layout.bugs_file, pkg / "bugs.jsonl")
            k = svc.knowledge()
        manifest = {
            "bugmind_version": __version__,
            "project": svc.cfg.storage.project,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "bugs": len(k.records),
            "patterns": len(k.patterns),
            "active_rules": len(k.active_rules),
            "candidate_rules": len(k.candidate_rules),
            "files": [*TARGETS.values(), "bugs.jsonl", "reports/",
                      *(["attachments/"] if layout.attachments_dir.exists() else [])],
        }
        atomic_write_text(pkg / "MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
        if as_zip:
            archive = shutil.make_archive(str(staging / base), "zip", root_dir=staging, base_dir=base)
            os.replace(archive, final)
        else:
            if final.exists():
                shutil.rmtree(final)  # only reached with --force
            os.replace(pkg, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return final
