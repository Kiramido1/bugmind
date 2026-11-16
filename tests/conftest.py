from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bugmind.config import AppConfig, set_value
from bugmind.service import BugMindService
from bugmind.storage.bug_store import BugStore
from bugmind.templates import TemplateEngine

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Never touch the real ~/.config, ~/.local/share or ~/.cache."""
    monkeypatch.setenv("BUGMIND_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BUGMIND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # Hide the developer's real agent CLIs (~/.local/bin/claude, ~/.opencode/bin/opencode): tests must
    # never call a real model, and provider auto-detection must not depend on this machine.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("FAKE_AGENT_RECORD", raising=False)
    for var in ("FAKE_AGENT_MODE", "FAKE_AGENT_RELATION", "FAKE_AGENT_MATCH", "FAKE_AGENT_PATTERN",
                "FAKE_AGENT_QUESTIONS", "FAKE_AGENT_RULE_TITLE", "FAKE_AGENT_RULE_ITEMS", "FAKE_AGENT_SEVERITY"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def cfg() -> AppConfig:
    c = AppConfig()
    c = set_value(c, "provider.default", "custom")
    data = c.model_dump(mode="json")
    data["provider"]["custom"]["command"] = [sys.executable, str(FAKE_AGENT)]
    data["provider"]["timeout_seconds"] = 20
    return AppConfig.model_validate(data)


@pytest.fixture
def service(cfg, tmp_path) -> BugMindService:
    return BugMindService(cfg, store=BugStore(tmp_path / "data" / "default"),
                          engine=TemplateEngine(tmp_path / "config" / "templates"))
