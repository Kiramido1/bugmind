"""Configuration: ~/.config/bugmind/config.yaml (XDG aware), validated with Pydantic.

Environment overrides (useful for tests and multiple workspaces):
  BUGMIND_CONFIG_DIR  - directory holding config.yaml, agents/, templates/
  BUGMIND_DATA_DIR    - storage root (overrides storage.data_dir)

Credentials never live here: Claude Code / OpenCode manage their own auth, and
custom agents should read secrets from their own environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, ValidationError

from bugmind.security import validate_slug
from bugmind.storage.atomic import atomic_write_text

ProviderName = Literal["claude", "opencode", "custom"]
PROVIDER_NAMES: tuple[str, ...] = ("claude", "opencode", "custom")
PROVIDER_LABELS = {"claude": "Claude Code", "opencode": "OpenCode", "custom": "Custom Agent"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ClaudeConfig(_Strict):
    executable: str = "claude"
    # Measured on a typical bug: sonnet + low effort is ~2.4x faster than the defaults, same report quality.
    model: str | None = "sonnet"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"  # --effort; None = Claude's default
    extra_args: list[str] = Field(default_factory=list)


class OpenCodeConfig(_Strict):
    executable: str = "opencode"
    model: str | None = None  # provider/model
    agent: str | None = None
    extra_args: list[str] = Field(default_factory=list)


class CustomConfig(_Strict):
    name: str = "Custom Agent"
    # argv list, e.g. ["/usr/local/bin/my-agent", "--json"]. Never run through a shell.
    # The token "{prompt_file}" in an argument is replaced by a temp file path holding the request.
    command: list[str] = Field(default_factory=list)
    input_mode: Literal["stdin_json", "stdin_text", "file"] = "stdin_json"
    env: dict[str, str] = Field(default_factory=dict)


class ProviderSettings(_Strict):
    default: ProviderName | None = None
    timeout_seconds: int = Field(default=300, ge=10, le=3600)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    custom: CustomConfig = Field(default_factory=CustomConfig)


class OutputSettings(_Strict):
    # All generated output is English, whatever the input language (Arabic input is supported).
    template: str = "default/professional"


class StorageSettings(_Strict):
    data_dir: str | None = None  # None -> $XDG_DATA_HOME/bugmind
    project: str = "default"
    backups_to_keep: int = Field(default=20, ge=1, le=500)

    @field_validator("project")
    @classmethod
    def _project(cls, v: str) -> str:
        return validate_slug(v, "project")


class AnalysisSettings(_Strict):
    duplicate_threshold: float = Field(default=0.88, ge=0.5, le=1.0)
    redact_secrets: bool = True
    # When should a successful analysis be persisted automatically in the TUI?
    #   complete: auto-save unless the agent asked clarification questions
    #   always:   always auto-save
    #   never:    always wait for /save
    auto_save: Literal["complete", "always", "never"] = "complete"


class TuiSettings(_Strict):
    theme: str = "bugmind-crimson"
    show_timestamps: bool = True


class AppConfig(_Strict):
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    tui: TuiSettings = Field(default_factory=TuiSettings)


# Friendly aliases for `bugmind config set <key> <value>`.
KEY_ALIASES = {
    "agent": "provider.default",
    "provider": "provider.default",
    "timeout": "provider.timeout_seconds",
    "template": "output.template",
    "project": "storage.project",
    "data_dir": "storage.data_dir",
    "auto_save": "analysis.auto_save",
}


def config_dir() -> Path:
    if env := os.environ.get("BUGMIND_CONFIG_DIR"):
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "bugmind"


def config_path() -> Path:
    return config_dir() / "config.yaml"


def default_data_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "bugmind"


def data_root(cfg: AppConfig) -> Path:
    if env := os.environ.get("BUGMIND_DATA_DIR"):
        return Path(env).expanduser()
    if cfg.storage.data_dir:
        return Path(cfg.storage.data_dir).expanduser()
    return default_data_root()


def project_dir(cfg: AppConfig) -> Path:
    return data_root(cfg) / cfg.storage.project


class ConfigError(Exception):
    pass


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.exists():
        return AppConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"{path}: {exc}") from exc


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    text = "# BugMind configuration. Edit with `bugmind config set <key> <value>`.\n"
    text += yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    atomic_write_text(path, text)
    return path


def resolve_key(key: str) -> list[str]:
    key = KEY_ALIASES.get(key, key)
    parts = key.split(".")
    if not all(parts):
        raise ConfigError(f"Invalid key: {key!r}")
    return parts


def get_value(cfg: AppConfig, key: str) -> Any:
    node: Any = cfg.model_dump(mode="json")
    for part in resolve_key(key):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"Unknown config key: {key}")
        node = node[part]
    return node


def set_value(cfg: AppConfig, key: str, raw_value: str) -> AppConfig:
    """Return a new validated config with *key* set. Values are parsed as YAML scalars/lists."""
    parts = resolve_key(key)
    data = cfg.model_dump(mode="json")
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            raise ConfigError(f"Unknown config key: {key}")
        node = node[part]
    if parts[-1] not in node:
        raise ConfigError(f"Unknown config key: {key}")
    try:
        value = yaml.safe_load(raw_value) if raw_value != "" else None
    except yaml.YAMLError:
        value = raw_value
    if isinstance(node[parts[-1]], str) and isinstance(value, (int, float, bool)):
        value = raw_value  # keep strings as strings
    if value in ("null", "none", "None"):
        value = None
    node[parts[-1]] = value
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:  # one short line per problem instead of pydantic's verbose dump
        raise ConfigError("; ".join(f"{key}: {e['msg']}" for e in exc.errors())) from exc
