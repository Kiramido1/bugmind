"""Common provider contract shared by every AI agent adapter."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from bugmind.images import Attachment

ChunkCallback = Callable[[str], None]


def as_dict(value: object) -> dict:
    """Agent output is untrusted: treat anything that is not a JSON object as empty."""
    return value if isinstance(value, dict) else {}


class ErrorKind(StrEnum):
    MISSING = "missing"  # executable not found / not configured
    AUTH = "auth"  # agent not logged in / bad credentials
    TIMEOUT = "timeout"
    PROCESS = "process"  # non-zero exit or crash
    PROTOCOL = "protocol"  # unparseable / empty output
    CANCELLED = "cancelled"


class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, message: str, provider: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.provider = provider

    def __str__(self) -> str:
        prefix = f"[{self.provider}] " if self.provider else ""
        return f"{prefix}{self.kind.value}: {self.args[0]}"


@dataclass
class ProviderRequest:
    system_prompt: str
    prompt: str
    timeout: float = 300.0
    images: list[Attachment] = field(default_factory=list)  # validated screenshots


@dataclass
class ProviderResult:
    text: str
    provider: str
    duration_s: float = 0.0
    cost_usd: float | None = None
    meta: dict[str, object] = field(default_factory=dict)


@dataclass
class ProviderStatus:
    name: str
    label: str
    available: bool
    version: str | None = None
    detail: str = ""


class Provider(ABC):
    name: str = "base"
    label: str = "Base"
    supports_streaming: bool = False

    @abstractmethod
    async def check(self) -> ProviderStatus:
        """Cheap availability check (executable present, version). Never calls the model."""

    @abstractmethod
    async def run(self, request: ProviderRequest, on_chunk: ChunkCallback | None = None) -> ProviderResult:
        """Execute one task. Raises ProviderError on any failure."""


_AUTH_HINTS = (
    "not logged in", "please run /login", "please log in", "login required", "authentication",
    "unauthorized", "invalid api key", "invalid x-api-key", "401", "credential", "oauth token",
)


def looks_like_auth_error(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _AUTH_HINTS)


def agent_workdir() -> Path:
    """An empty, BugMind-owned working directory for agent processes.

    Running agents here prevents them from picking up project files or
    instructions (e.g. CLAUDE.md / AGENTS.md) from wherever bugmind was launched.
    """
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    path = Path(base) / "bugmind" / "agent-workdir"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path
