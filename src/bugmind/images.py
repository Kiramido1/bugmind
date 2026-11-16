"""Screenshot attachments: validated by content (magic bytes), size-capped, never executed.

The agent never reads files itself (tools stay disabled): BugMind reads and validates each image
and hands the bytes to the provider adapter.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

MAX_IMAGES = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Anthropic's per-image API limit

_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]

# Paths as GNOME Terminal pastes them on drag & drop ('quoted' when they contain spaces) or typed.
_PATH_RE = re.compile(
    r"""'([^'\n]+\.(?:png|jpe?g|gif|webp))'|"([^"\n]+\.(?:png|jpe?g|gif|webp))"|((?:~|/)[^\s'"]+\.(?:png|jpe?g|gif|webp))""",
    re.IGNORECASE)


EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


class ImageError(ValueError):
    pass


@dataclass(frozen=True)
class Attachment:
    path: Path
    media_type: str
    size: int

    @property
    def name(self) -> str:
        return self.path.name

    def digest(self) -> str:
        """Content hash: the same screenshot pasted twice is attached once."""
        import hashlib
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def read_base64(self) -> str:
        return base64.b64encode(self.path.read_bytes()).decode("ascii")

    @property
    def display_name(self) -> str:
        return "clipboard image" if self.name.startswith("clipboard-") else self.name

    def label(self) -> str:
        return f"{self.display_name} ({self.size / 1024:.0f} KB)"


def sniff(head: bytes) -> str | None:
    for magic, media in _MAGIC:
        if head.startswith(magic):
            return media
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def load_image(raw: str | Path) -> Attachment:
    """Validate one screenshot path. Raises ImageError with a user-facing message."""
    path = Path(str(raw).strip()).expanduser()
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ImageError(f"Image not found: {raw}") from None
    if not path.is_file():
        raise ImageError(f"Not a file: {path}")
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ImageError(f"{path.name} is {size / 1048576:.1f} MB; the limit is {MAX_IMAGE_BYTES // 1048576} MB.")
    with path.open("rb") as fh:
        media = sniff(fh.read(16))
    if media is None:
        raise ImageError(f"{path.name} is not a PNG, JPEG, GIF or WebP image.")
    return Attachment(path, media, size)


def check_count(images: list[Attachment]) -> None:
    if len(images) > MAX_IMAGES:
        raise ImageError(f"At most {MAX_IMAGES} screenshots per bug ({len(images)} given).")


def extract_images(text: str) -> tuple[str, list[Attachment]]:
    """Pull existing local image paths out of a message (drag & drop pastes the path).

    Paths that don't exist locally are left untouched: "/static/logo.png returns 404" is bug text.
    """
    found: list[Attachment] = []

    def repl(m: re.Match[str]) -> str:
        candidate = next(g for g in m.groups() if g)
        try:
            img = load_image(candidate)
        except ImageError:
            return m.group(0)
        found.append(img)
        return ""

    cleaned = _PATH_RE.sub(repl, text)
    # tidy the gaps left behind, but keep the user's line breaks
    tidy = "\n".join(re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in cleaned.split("\n")).strip()
    return (tidy if found else text), found


# ----------------------------------------------------------------------------- clipboard (Ctrl+V)

_CLIP_IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"]
_FILE_LIST_TYPES = ["x-special/gnome-copied-files", "text/uri-list"]  # "Copy" on a file in Nautilus etc.
PASTE_RETENTION_S = 7 * 24 * 3600


def _clipboard_tools() -> tuple[list[str], list[str]] | None:
    """(list-types argv, read-type argv prefix) for the session's clipboard; fixed argv, never a shell."""
    import os
    import shutil
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return ["wl-paste", "--list-types"], ["wl-paste", "--no-newline", "--type"]
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"], ["xclip", "-selection", "clipboard", "-o", "-t"]
    return None


def _run(argv: list[str]) -> bytes | None:
    import subprocess
    try:
        out = subprocess.run(argv, capture_output=True, timeout=3, check=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def paste_dir() -> Path:
    import os
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "bugmind" / "pastes"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def _save_paste(data: bytes, media: str) -> Attachment:
    import os
    import time
    folder = paste_dir()
    for old in folder.glob("clipboard-*"):  # pasted images are only needed until the bug is saved
        try:
            if time.time() - old.stat().st_mtime > PASTE_RETENTION_S:
                old.unlink()
        except OSError:
            pass
    path = folder / f"clipboard-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{time.monotonic_ns() % 10**6}{EXTENSIONS[media]}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return load_image(path)  # same validation as any other image


def read_clipboard() -> tuple[list[Attachment], str | None]:
    """What Ctrl+V should insert: (images, None) for an image or copied image files, ([], text) for
    text, ([], None) when the clipboard is empty/unavailable. Raises ImageError for a bad image."""
    tools = _clipboard_tools()
    if tools is None:
        return [], None
    list_argv, read_argv = tools
    types = (_run(list_argv) or b"").decode("utf-8", "replace").split()
    for media in _CLIP_IMAGE_TYPES:
        if media in types:
            data = _run([*read_argv, media])
            if not data:
                break
            if len(data) > MAX_IMAGE_BYTES:
                raise ImageError(f"Clipboard image is {len(data) / 1048576:.1f} MB; the limit is "
                                 f"{MAX_IMAGE_BYTES // 1048576} MB.")
            if sniff(data[:16]) is None:
                raise ImageError("The clipboard image is not a PNG, JPEG, GIF or WebP.")
            return [_save_paste(data, sniff(data[:16]) or media)], None
    for kind in _FILE_LIST_TYPES:
        if kind in types:
            from urllib.parse import unquote, urlparse
            raw = (_run([*read_argv, kind]) or b"").decode("utf-8", "replace")
            files = [unquote(urlparse(ln.strip()).path) for ln in raw.splitlines() if ln.strip().startswith("file://")]
            images = []
            for f in files:
                try:
                    images.append(load_image(f))
                except ImageError:
                    continue  # a copied non-image file falls through to text paste
            if images:
                return images, None
    for kind in ("UTF8_STRING", "text/plain;charset=utf-8", "text/plain", "STRING", "TEXT"):
        if kind in types:
            data = _run([*read_argv, kind])
            if data is not None:
                return [], data.decode("utf-8", "replace")
    return [], None
