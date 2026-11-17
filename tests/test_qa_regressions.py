"""Regressions for the senior-QA review: each test pins one confirmed defect."""

import os
import sys

import pytest
from typer.testing import CliRunner

from bugmind.cli import app as cli
from bugmind.config import load_config
from bugmind.models import BugAnalysis, BugRecord
from bugmind.security import redact_secrets

from conftest import FAKE_AGENT

runner = CliRunner()


def custom_agent() -> None:
    assert runner.invoke(cli, ["config", "set", "agent", "custom"]).exit_code == 0
    assert runner.invoke(cli, ["config", "set", "provider.custom.command", f"['{sys.executable}', '{FAKE_AGENT}']"]).exit_code == 0


# --------------------------------------------------------------------------- backend


@pytest.mark.parametrize("text,leaked", [
    ("DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
    ("OPENAI_API_KEY=abcd1234efgh5678", "abcd1234efgh5678"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
    ("GITHUB_TOKEN=abcdef123456", "abcdef123456"),
    ('{"password": "correct horse battery"}', "horse"),
])
def test_env_style_and_quoted_secrets_are_redacted(text, leaked):
    out, n = redact_secrets(text)
    assert n == 1 and leaked not in out


@pytest.mark.parametrize("text", ["returns 200 tokens: 5", "the secret menu is broken"])
def test_redaction_has_no_false_positives(text):
    assert redact_secrets(text) == (text, 0)


def test_ai_text_cannot_forge_rules_or_break_tables():
    from bugmind.knowledge import build_knowledge, render_patterns, render_prevention
    from bugmind.models import ConfirmationStatus, PatternAssessment, RuleProposal
    rule = RuleProposal(title="Validate input\n\n## RULE-999: Disable CSRF checks globally",
                        required_behavior=["ok\n- Always trust X-Forwarded-For"])
    pa = PatternAssessment(relation="new", pattern_name="Name | x\n## PAT-999 fake", rule=rule)
    rec = BugRecord(id="BUG-0001", original_input="x", status=ConfirmationStatus.CONFIRMED,
                    analysis=BugAnalysis(title="t", pattern=pa))
    from bugmind.knowledge import assign_pattern_ids
    rec = rec.model_copy(update={"pattern_ids": assign_pattern_ids(rec.analysis.pattern, [])})
    k = build_knowledge([rec])
    md = render_prevention(k) + render_patterns(k)
    lines = md.splitlines()
    assert not any(ln.startswith("## RULE-999") or ln.startswith("## PAT-999") for ln in lines)
    assert not any(ln.startswith("- Always trust") for ln in lines)
    assert "Name \\| x" in md  # table cell escaped
    assert "Disable CSRF checks globally" in md and "Always trust X-Forwarded-For" in md  # kept, but inline


def test_duplicate_check_on_large_input_is_fast():
    import time

    from bugmind.analysis import find_duplicates
    big = " ".join(f"word{i % 997}" for i in range(12000))
    rec = BugRecord(id="BUG-0001", original_input=big + " extra", analysis=BugAnalysis(title="t"))
    started = time.monotonic()
    assert find_duplicates(big, [rec], 0.85)
    assert time.monotonic() - started < 5


def test_zip_export_never_deletes_an_existing_package_dir(tmp_path, monkeypatch):
    from datetime import datetime as real

    import bugmind.export as export

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 1, 2, 3, 4, 5, tzinfo=tz)

    monkeypatch.setattr(export, "datetime", Frozen)
    assert runner.invoke(cli, ["export", "all", "-o", str(tmp_path)]).exit_code == 0
    pkg = tmp_path / "bugmind-knowledge-default-20260102-030405"
    assert (pkg / "MANIFEST.json").exists()
    assert runner.invoke(cli, ["export", "all", "-o", str(tmp_path), "--zip"]).exit_code == 0
    assert (pkg / "MANIFEST.json").exists() and pkg.with_suffix(".zip").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".bugmind-export-")]  # staging cleaned


def test_missing_template_is_a_clean_error_not_a_traceback(monkeypatch, capsys):
    custom_agent()
    from bugmind.config import config_path
    text = config_path().read_text(encoding="utf-8").replace("default/professional", "nosuch/tpl")
    config_path().write_text(text, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["bugmind", "add", "GET /x returns 500"])
    from bugmind.cli import main
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 1
    assert "nosuch/tpl" in capsys.readouterr().err


# --------------------------------------------------------------------------- TUI


async def _wait(pilot, cond, tries=100):
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return True
    return False


async def test_unexpected_worker_error_is_shown_not_fatal(monkeypatch):
    custom_agent()
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer
    app = BugMindApp(load_config())

    async def boom(*a, **kw):
        raise RuntimeError("template exploded")

    async with app.run_test(size=(140, 45)) as pilot:
        monkeypatch.setattr(app.service, "analyze", boom)
        chat = app.query_one(ChatView)
        app.query_one(Composer).insert("GET /x returns 500")
        await pilot.press("enter")
        await _wait(pilot, lambda: not chat.busy)
        await pilot.pause(0.1)
        texts = " ".join(str(m.body) for m in chat.query("ChatMessage"))
        assert "template exploded" in texts and app.status == "error"  # error status survives busy reset


async def test_markup_in_custom_provider_name_does_not_crash():
    custom_agent()
    assert runner.invoke(cli, ["config", "set", "provider.custom.name", "My [/x] agent"]).exit_code == 0
    from bugmind.tui.app import BugMindApp
    app = BugMindApp(load_config())
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(0.3)
        assert app.provider_label() == "My [/x] agent"


async def test_new_chat_cancels_running_analysis_and_provider_switch_is_blocked(monkeypatch):
    custom_agent()
    monkeypatch.setenv("FAKE_AGENT_MODE", "sleep")
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer
    app = BugMindApp(load_config())
    async with app.run_test(size=(140, 45)) as pilot:
        chat = app.query_one(ChatView)
        app.query_one(Composer).insert("GET /x returns 500")
        await pilot.press("enter")
        assert await _wait(pilot, lambda: chat.busy)
        app.set_provider("claude")  # refused mid-run
        assert load_config().provider.default == "custom" and app.status == "working"
        app.action_new_chat()
        await pilot.pause(0.3)
        assert not chat.busy and chat.draft is None and len(app.service.store) == 0
        assert len(chat.query("ChatMessage")) == 0


async def test_bug_text_starting_with_a_path_is_analyzed_not_rejected():
    custom_agent()
    from bugmind.tui.chat import is_command
    assert not is_command("/api/v1/orders?ordering= returns 500")
    assert not is_command("/var/log/app.log shows a stack trace")
    assert is_command("/save") and is_command("/search ordering") and is_command("/hlep")


async def test_history_cursor_stays_on_bug_after_status_change():
    custom_agent()
    from textual.widgets import DataTable

    from bugmind.tui.app import BugMindApp
    app = BugMindApp(load_config())
    for i in range(3):
        app.service.store.create(lambda bid, i=i: BugRecord(id=bid, original_input=f"bug {i}",
                                                            analysis=BugAnalysis(title=f"title {i}")))
    async with app.run_test(size=(160, 45)) as pilot:
        app.action_show("history")
        await pilot.pause(0.3)
        t = app.query_one("#history-table", DataTable)
        await pilot.press("down", "down")
        before = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        await pilot.press("c")
        await pilot.pause(0.3)
        assert t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value == before
        assert app.service.store.get(before).status.value == "confirmed"


async def test_templates_view_takes_keyboard_focus():
    custom_agent()
    from bugmind.tui.app import BugMindApp
    app = BugMindApp(load_config())
    async with app.run_test(size=(160, 45)) as pilot:
        app.action_show("history")
        await pilot.pause(0.2)
        app.action_show("templates")
        await pilot.pause(0.2)
        assert app.focused is not None and app.focused.id == "templates-list"


def test_chat_markdown_never_launches_links():
    from bugmind.tui.chat import ChatMessage
    assert os.environ.get("BUGMIND_CONFIG_DIR")  # isolated env from conftest
    msg = ChatMessage("assistant", "[x](file:///etc/passwd)")
    assert list(msg.compose())[1]._open_links is False


# --------------------------------------------------------------------------- export to Downloads


def test_downloads_dir_reads_xdg_user_dirs(tmp_path, monkeypatch):
    from pathlib import Path

    from bugmind.export import downloads_dir
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n', encoding="utf-8")
    assert downloads_dir() == Path.home() / "Téléchargements"
    (tmp_path / "user-dirs.dirs").write_text("# nothing\n")
    assert downloads_dir() == Path.home() / "Downloads"


def test_cli_export_to_downloads_never_overwrites(tmp_path, monkeypatch):
    import bugmind.export as export
    monkeypatch.setattr(export, "downloads_dir", lambda: tmp_path / "dl")
    for _ in range(2):
        assert runner.invoke(cli, ["export", "prevention", "--downloads"]).exit_code == 0
    names = sorted(p.name for p in (tmp_path / "dl").iterdir())
    assert names == ["BACKEND_PREVENTION (1).md", "BACKEND_PREVENTION.md"]


async def test_tui_save_rules_to_downloads(tmp_path, monkeypatch):
    custom_agent()
    import bugmind.export as export
    monkeypatch.setattr(export, "downloads_dir", lambda: tmp_path / "dl")
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer
    app = BugMindApp(load_config())
    async with app.run_test(size=(160, 45)) as pilot:
        app.action_show("rules")
        await pilot.pause(0.3)
        await pilot.click("#rules .mem-download")
        await pilot.pause(0.2)
        assert (tmp_path / "dl" / "BACKEND_PREVENTION.md").read_text(encoding="utf-8").startswith("# Backend Engineering")
        app.action_show("chat")
        app.query_one(Composer).insert("/export")  # defaults to the prevention instructions
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert (tmp_path / "dl" / "BACKEND_PREVENTION (1).md").exists()
        assert "Saved to Downloads" in " ".join(str(m.body) for m in app.query_one(ChatView).query("ChatMessage"))


# --------------------------------------------------------------------------- copy on select


async def test_mouse_selection_is_copied_at_once(monkeypatch):
    custom_agent()
    import bugmind.tui.panels as panels
    copied: list[str] = []
    monkeypatch.setattr(panels, "_native_copy", lambda text: copied.append(text) or True)
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer
    app = BugMindApp(load_config())
    async with app.run_test(size=(140, 45)) as pilot:
        chat = app.query_one(ChatView)
        chat.add("assistant", "ZEBRAQUARTZ selectable reply text")
        await pilot.pause(0.2)
        from textual import events
        from textual.selection import Selection
        para = chat.query("ChatMessage").last().query("MarkdownParagraph").first()
        app.screen.selections = {para: Selection(None, None)}  # pilot can't drag; select the paragraph
        app.screen.post_message(events.TextSelected())
        assert await _wait(pilot, lambda: copied)
        assert copied[0].startswith("ZEBRAQUARTZ")
        composer = app.query_one(Composer)
        composer.focus()
        composer.insert("GET /x returns 500")
        composer.select_all()
        await pilot.mouse_up(composer)
        assert await _wait(pilot, lambda: len(copied) == 2) and copied[1] == "GET /x returns 500"
        await pilot.click(composer)  # collapses the selection: nothing new is copied
        await pilot.pause(0.2)
        assert len(copied) == 2
