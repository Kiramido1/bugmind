"""AI provider adapters. Use `get_provider(cfg)` to obtain the configured adapter."""

from __future__ import annotations

from bugmind.config import PROVIDER_LABELS, PROVIDER_NAMES, AppConfig
from bugmind.providers.base import (
    ErrorKind, Provider, ProviderError, ProviderRequest, ProviderResult, ProviderStatus,
)


def get_provider(cfg: AppConfig, name: str | None = None) -> Provider:
    name = name or cfg.provider.default
    if name is None:
        raise ProviderError(ErrorKind.MISSING, "No AI provider selected. Run `bugmind config set agent claude|opencode|custom`.")
    if name == "claude":
        from bugmind.providers.claude import ClaudeCodeProvider
        return ClaudeCodeProvider(cfg.provider.claude)
    if name == "opencode":
        from bugmind.providers.opencode import OpenCodeProvider
        return OpenCodeProvider(cfg.provider.opencode)
    if name == "custom":
        from bugmind.providers.custom import CustomProvider
        return CustomProvider(cfg.provider.custom)
    raise ProviderError(ErrorKind.MISSING, f"Unknown provider {name!r}; choose one of {', '.join(PROVIDER_NAMES)}")


AUTO_ORDER = ("claude", "opencode")  # what "just works" when the agent CLI is installed


def usable(cfg: AppConfig, name: str | None) -> bool:
    """Can *name* run here? Only resolves the executable on PATH; nothing is executed."""
    from bugmind.providers.process import resolve_executable
    if name == "custom":
        command = cfg.provider.custom.command
        executable = command[0] if command else ""
    elif name in ("claude", "opencode"):
        executable = getattr(cfg.provider, name).executable
    else:
        return False
    try:
        resolve_executable(executable, name)
    except ProviderError:
        return False
    return True


def auto_provider(cfg: AppConfig) -> str | None:
    """The configured provider if it can run, else the first installed agent CLI (Claude Code, OpenCode)."""
    if usable(cfg, cfg.provider.default):
        return cfg.provider.default
    return next((n for n in AUTO_ORDER if usable(cfg, n)), None)


__all__ = [
    "PROVIDER_LABELS", "PROVIDER_NAMES", "ErrorKind", "Provider", "ProviderError", "ProviderRequest",
    "ProviderResult", "ProviderStatus", "auto_provider", "get_provider", "usable",
]
