"""CLI commands and the TUI chat flow, driven against the fake agent."""

import sys

from typer.testing import CliRunner

from bugmind.cli import app
from bugmind.config import load_config

from conftest import FAKE_AGENT

runner = CliRunner()


def setup_custom_agent():
    assert runner.invoke(app, ["config", "set", "agent", "custom"]).exit_code == 0
    r = runner.invoke(app, ["config", "set", "provider.custom.command", f"['{sys.executable}', '{FAKE_AGENT}']"])
    assert r.exit_code == 0, r.output


def test_config_set_get_and_validation():
    r = runner.invoke(app, ["config", "set", "agent", "claude"])
    assert r.exit_code == 0 and load_config().provider.default == "claude"
    assert runner.invoke(app, ["config", "set", "agent", "gpt"]).exit_code == 2
    assert runner.invoke(app, ["config", "set", "nope.key", "1"]).exit_code == 2
    assert runner.invoke(app, ["config", "set", "project", "../x"]).exit_code == 2
    assert runner.invoke(app, ["config", "set", "timeout", "600"]).exit_code == 0
    assert "600" in runner.invoke(app, ["config", "get", "provider.timeout_seconds"]).output


def test_init_and_full_cli_workflow(tmp_path):
    assert runner.invoke(app, ["init", "--provider", "custom"]).exit_code == 0
    setup_custom_agent()
    r = runner.invoke(app, ["add"], input="Empty ordering on GET /api/v1/orders?ordering= returns 500\n")
    assert r.exit_code == 0, r.output
    assert "Saved BUG-0001" in r.output
    r = runner.invoke(app, ["add"], input="Empty ordering on GET /api/v1/orders?ordering= returns 500\n")
    assert r.exit_code == 3  # exact duplicate refused
    assert "BUG-0001" in runner.invoke(app, ["list"]).output
    assert "Orders API" in runner.invoke(app, ["show", "BUG-0001"]).output
    assert runner.invoke(app, ["show", "BUG-0999"]).exit_code == 1
    assert "BUG-0001" in runner.invoke(app, ["search", "ordering"]).output
    assert "RULE-001" in runner.invoke(app, ["memory"]).output
    assert "PAT-001" in runner.invoke(app, ["patterns"]).output
    assert "Total recorded bugs" in runner.invoke(app, ["stats"]).output
    assert runner.invoke(app, ["status", "BUG-0001", "confirmed"]).exit_code == 0
    assert "Candidate Rules" not in runner.invoke(app, ["memory"]).output
    assert runner.invoke(app, ["memory", "--rebuild"]).exit_code == 0
    r = runner.invoke(app, ["export", "all", "-o", str(tmp_path), "--zip"])
    assert r.exit_code == 0 and list(tmp_path.glob("bugmind-knowledge-*.zip"))
    assert "default/professional" in runner.invoke(app, ["templates"]).output


def test_add_needs_info_without_yes_is_not_saved(monkeypatch):
    setup_custom_agent()
    monkeypatch.setenv("FAKE_AGENT_QUESTIONS", '["Which role?"]')
    r = runner.invoke(app, ["add", "bug text here"])
    assert r.exit_code == 4 and "Which role?" in r.output
    r = runner.invoke(app, ["add", "--yes", "bug text here"])
    assert r.exit_code == 0 and "needs_info" in r.output


def test_add_provider_failure_exit_code(monkeypatch):
    setup_custom_agent()
    monkeypatch.setenv("FAKE_AGENT_MODE", "fail")
    r = runner.invoke(app, ["add", "x y z"])
    assert r.exit_code == 1 and "Analysis failed" in r.output


# ---------------------------------------------------------------------- TUI


async def test_tui_chat_analyzes_saves_and_updates_views(monkeypatch):
    setup_custom_agent()
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer

    app = BugMindApp(load_config())
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        composer.insert("Empty ordering on GET /api/v1/orders?ordering= returns 500")
        await pilot.press("enter")
        chat = app.query_one(ChatView)
        for _ in range(100):
            await pilot.pause(0.1)
            if not chat.busy and len(app.service.store) == 1:
                break
        assert len(app.service.store) == 1  # auto-saved (no open questions)
        texts = " ".join(str(m.body) for m in chat.query("ChatMessage"))
        assert "Saved **BUG-0001**" in texts
        await pilot.press("ctrl+h")
        await pilot.pause(0.3)
        from textual.widgets import DataTable
        assert app.query_one("#history-table", DataTable).row_count == 1
        app.action_show("rules")
        await pilot.pause(0.3)
        assert app.query_one("#main").current == "rules"


async def test_tui_draft_with_questions_waits_for_save(monkeypatch):
    setup_custom_agent()
    monkeypatch.setenv("FAKE_AGENT_QUESTIONS", '["Which role?"]')
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer

    app = BugMindApp(load_config())
    async with app.run_test(size=(140, 45)) as pilot:
        chat = app.query_one(ChatView)
        chat.mode = "bug"
        composer = app.query_one(Composer)
        composer.insert("orders endpoint returns 500")
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.1)
            if chat.draft is not None:
                break
        assert chat.draft is not None and len(app.service.store) == 0
        composer.insert("/save")
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert len(app.service.store) == 1
        assert app.service.store.all()[0].status.value == "needs_info"


async def test_tui_first_run_prompts_for_provider_when_no_agent_is_installed():
    from bugmind.tui.app import BugMindApp, ProviderSelectScreen

    app = BugMindApp(load_config())  # no provider configured, no agent CLI on PATH
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, ProviderSelectScreen)
        await pilot.press("2")  # OpenCode is not installed -> refused, nothing saved
        await pilot.pause(0.2)
        assert load_config().provider.default is None


def _install_fake_cli(tmp_path, monkeypatch, name):
    import os
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


async def test_installed_agent_is_used_automatically(tmp_path, monkeypatch):
    """Regression: header said Claude Code while the unconfigured custom provider actually ran."""
    from bugmind.tui.app import BugMindApp, ProviderSelectScreen
    _install_fake_cli(tmp_path, monkeypatch, "opencode")
    runner.invoke(app, ["config", "set", "agent", "custom"])  # custom without a command = unusable
    bm = BugMindApp(load_config())
    async with bm.run_test() as pilot:
        await pilot.pause(0.3)
        assert not isinstance(bm.screen, ProviderSelectScreen)
        assert load_config().provider.default == "opencode" and bm.provider_label() == "OpenCode"
        bm.set_provider("custom")  # still no command -> refused, stays on OpenCode
        assert load_config().provider.default == "opencode"


def test_cli_init_and_add_detect_installed_agent(tmp_path, monkeypatch):
    _install_fake_cli(tmp_path, monkeypatch, "claude")
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and load_config().provider.default == "claude"


def test_command_matching():
    from bugmind.tui.chat import STATIC_COMMANDS, match_commands
    names = [c.name for c in match_commands("/prov", STATIC_COMMANDS)]
    assert names[:4] == ["/provider", "/provider claude", "/provider opencode", "/provider custom"]
    assert [c.name for c in match_commands("/export p", STATIC_COMMANDS)] == ["/export prevention", "/export patterns"]
    assert [c.name for c in match_commands("/zip", STATIC_COMMANDS)] == ["/export all"]  # description match
    assert match_commands("/nothing-matches", STATIC_COMMANDS) == []


async def test_tui_slash_menu_switches_provider_and_navigates(tmp_path, monkeypatch):
    setup_custom_agent()
    _install_fake_cli(tmp_path, monkeypatch, "claude")
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer

    app = BugMindApp(load_config())
    async with app.run_test(size=(140, 45)) as pilot:
        chat, composer = app.query_one(ChatView), app.query_one(Composer)
        composer.focus()
        await pilot.press("slash")
        await pilot.pause(0.1)
        assert chat.menu_open
        for key in "prov":
            await pilot.press(key)
        await pilot.press("down", "enter")  # "/provider claude"
        await pilot.pause(0.2)
        assert load_config().provider.default == "claude" and composer.text == "" and not chat.menu_open
        for key in "/sta":
            await pilot.press(key)
        await pilot.press("tab")
        assert composer.text == "/stats"
        await pilot.press("escape")
        assert not chat.menu_open
        await pilot.press("enter")  # menu closed -> submit typed command
        await pilot.pause(0.3)
        assert app.query_one("#main").current == "stats"
        app.action_show("chat")
        composer.insert("/search ordering")
        await pilot.press("enter")
        await pilot.pause(0.3)
        from textual.widgets import Input
        assert app.query_one("#main").current == "history"
        assert app.query_one("#history-search", Input).value == "ordering"


async def test_tui_rapid_new_chat_does_not_crash():
    """Regression: double-clicking "New Chat" mounted two widgets with id 'hero' (DuplicateIds)."""
    setup_custom_agent()
    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView

    app = BugMindApp(load_config())
    async with app.run_test(size=(140, 45)) as pilot:
        chat = app.query_one(ChatView)
        for _ in range(5):
            chat.clear()
        app.action_new_chat()
        app.action_new_chat()
        await pilot.pause(0.3)
        await pilot.click("#sidebar", offset=(5, 2), times=2)
        await pilot.pause(0.3)
        assert len(chat.query(".hero")) == 1


async def test_tui_theme_choice_applies_and_persists():
    setup_custom_agent()
    from textual.widgets import Select

    from bugmind.tui.app import BugMindApp

    app = BugMindApp(load_config())
    async with app.run_test(size=(140, 45)) as pilot:
        assert app.theme == "bugmind-crimson"
        app.action_show("settings")
        await pilot.pause(0.3)
        app.query_one("#set-theme", Select).value = "bugmind-light"
        await pilot.pause(0.2)
        assert app.theme == "bugmind-light"  # live preview
        await pilot.click("#settings-save")
        await pilot.pause(0.3)
    assert load_config().tui.theme == "bugmind-light"


async def test_tui_workspace_panels_survive_markup_like_titles():
    """Bug titles such as "[API / Orders] …" must render literally in the sidebar and context panel."""
    setup_custom_agent()
    from textual.widgets import OptionList

    from bugmind.tui.app import BugMindApp

    app_ = BugMindApp(load_config())
    from bugmind.models import BugAnalysis, BugRecord
    app_.service.store.create(lambda bug_id: BugRecord(id=bug_id, original_input="x",
                                                       analysis=BugAnalysis(title="[API / Orders] [/] broken [b")))
    async with app_.run_test(size=(170, 45)) as pilot:
        await pilot.pause(0.3)
        recent = app_.query_one("#recent", OptionList)
        assert recent.option_count >= 1
        assert app_.query_one("#context").display and app_.query_one("#sidebar-pane").display
        await pilot.resize_terminal(90, 40)
        await pilot.pause(0.2)
        assert not app_.query_one("#context").display and not app_.query_one("#sidebar-pane").display
        app_.action_show("history")
        await pilot.pause(0.3)


async def test_tui_draft_buttons_recall_copy_and_bug_popup(monkeypatch):
    setup_custom_agent()
    monkeypatch.setenv("FAKE_AGENT_QUESTIONS", '["Which role?"]')
    copied = []
    import bugmind.tui.panels as panels
    monkeypatch.setattr(panels, "copy_text", lambda app, text, what="Report": copied.append(text))
    from textual.widgets import OptionList

    from bugmind.tui.app import BugMindApp
    from bugmind.tui.chat import ChatView, Composer
    from bugmind.tui.panels import BugDetailScreen

    app_ = BugMindApp(load_config())
    async with app_.run_test(size=(170, 50)) as pilot:
        chat, composer = app_.query_one(ChatView), app_.query_one(Composer)
        composer.focus()
        composer.insert("orders endpoint returns 500")
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.1)
            if chat.draft is not None:
                break
        await pilot.pause(0.2)
        assert chat.draft is not None and len(app_.service.store) == 0  # open question -> waits
        # ↑ recalls the previous message
        await pilot.press("up")
        assert composer.text == "orders endpoint returns 500"
        composer.clear()
        await pilot.click(".act-copy")
        assert copied and "Bug Title" in copied[-1]
        await pilot.click(".act-save")
        await pilot.pause(0.3)
        assert len(app_.service.store) == 1 and chat.draft is None
        assert not chat.query(".act-save")  # stale draft buttons removed
        # recent list -> popup -> confirm
        recent = app_.query_one("#recent", OptionList)
        assert recent.option_count == 1
        app_.open_bug("BUG-0001")
        await pilot.pause(0.3)
        assert isinstance(app_.screen, BugDetailScreen)
        await pilot.press("c")
        await pilot.pause(0.2)
        assert app_.service.store.get("BUG-0001").status.value == "confirmed"
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app_.screen, BugDetailScreen)


def test_cli_untrusted_text_never_breaks_markup_and_inputs_are_validated():
    """QA regressions: '[/]' in ids/queries/titles crashed Rich; bad init/template values were accepted."""
    setup_custom_agent()
    for args in (["show", "[/]"], ["status", "[/]", "confirmed"], ["search", "[/x]"], ["config", "set", "[/]", "1"],
                 ["export", "[/]"]):
        r = runner.invoke(app, args)
        assert r.exception is None or isinstance(r.exception, SystemExit), (args, r.output)
        assert "[/" in r.output, args  # shown literally
    r = runner.invoke(app, ["init", "--provider", "nope"])
    assert r.exit_code == 2 and "Traceback" not in r.output
    assert runner.invoke(app, ["config", "set", "template", "../../x"]).exit_code == 2
    assert runner.invoke(app, ["config", "set", "template", "nope/x"]).exit_code == 2
    assert load_config().output.template == "default/professional"
    r = runner.invoke(app, ["config", "set", "timeout", "abc"])
    assert r.exit_code == 2 and "errors.pydantic.dev" not in r.output
    # a markup-like title from the agent renders literally in list/show, and ids are case-insensitive
    r = runner.invoke(app, ["add", "--yes", "GET /x [/] returns 500"])
    assert r.exit_code == 0, r.output
    assert runner.invoke(app, ["show", "bug-0001", "--original"]).exit_code == 0
    assert "[/]" in runner.invoke(app, ["show", "BUG-0001", "--original"]).output


def test_oversized_input_is_refused_before_calling_the_agent():
    setup_custom_agent()
    from bugmind.service import MAX_INPUT_CHARS
    r = runner.invoke(app, ["add", "x" * (MAX_INPUT_CHARS + 1)])
    assert r.exit_code == 1 and "limit" in r.output
