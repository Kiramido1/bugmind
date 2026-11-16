"""BugMind Textual application: header, sidebar navigation, content views, footer."""

from __future__ import annotations

from pathlib import Path

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import ContentSwitcher, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from bugmind.config import PROVIDER_LABELS, PROVIDER_NAMES, AppConfig, ConfigError, load_config, save_config, set_value
from bugmind.providers import auto_provider, usable
from bugmind.service import BugMindService
from bugmind.tui.chat import HELP_TEXT, ChatView, Composer
from bugmind.tui.panels import (BugDetailScreen, ContextPanel, NewChatButton, Rail, StatusBar, TopBar, copy_text,
                                recent_options)
from bugmind.tui.views import HistoryView, MemoryView, ProvidersView, SettingsView, TemplatesView

# Calm, low-glare palettes: one soft accent colour, desaturated status colours,
# moderate contrast (foreground ≈ 10:1, muted text ≈ 4.6:1 on the background).
# Default: dark red on black, taken from the user's GNOME Terminal palette
# (background #000000, text #AAAAAA, red #C01C28 / #F66151).
CRIMSON_THEME = Theme(
    name="bugmind-crimson",
    primary="#C8323C",      # dark red (terminal red, a touch lighter for legibility)
    secondary="#B08A8A",    # dusty rose
    accent="#C061CB",
    warning="#E9AD0C",
    error="#F66151",
    success="#33D17A",
    foreground="#BDB6B6",
    background="#000000",
    surface="#0C0808",
    panel="#1C1012",
    dark=True,
    variables={
        "text-muted": "#7A6E6F",
        "input-selection-background": "#C01C28 35%",
        "block-cursor-background": "#C8323C",
        "block-cursor-foreground": "#000000",
        "scrollbar": "#1C1012",
        "scrollbar-hover": "#2E1A1D",
        "scrollbar-active": "#C8323C",
        "scrollbar-background": "#000000",
        "markdown-h1-color": "#E8656B",
        "markdown-h2-color": "#E8656B",
        "markdown-h3-color": "#BDB6B6",
        "link-color": "#F66151",
    },
)

BUGMIND_THEME = Theme(
    name="bugmind-dark",
    primary="#4FC1B9",      # soft teal
    secondary="#7AA2E8",    # muted blue
    accent="#A893E0",
    warning="#E3A262",
    error="#E0707A",
    success="#4CC38A",
    foreground="#C8D3DC",
    background="#0B141B",
    surface="#0F1A23",
    panel="#16242F",
    dark=True,
    variables={
        "text-muted": "#6E808E",
        "input-selection-background": "#4FC1B9 28%",
        "block-cursor-background": "#4FC1B9",
        "block-cursor-foreground": "#0B141B",
        "scrollbar": "#16242F",
        "scrollbar-hover": "#22343F",
        "scrollbar-active": "#4FC1B9",
        "scrollbar-background": "#0B141B",
        "markdown-h1-color": "#6FD3CB",
        "markdown-h2-color": "#6FD3CB",
        "markdown-h3-color": "#C8D3DC",
        "link-color": "#8AB4F8",
    },
)

SLATE_THEME = Theme(
    name="bugmind-slate",
    primary="#6FA3D8",      # soft steel blue
    secondary="#7FB5A8",    # sage
    accent="#A99BD4",       # muted lavender
    warning="#D4A85A",
    error="#D9727A",
    success="#82B98A",
    foreground="#C5CDD8",
    background="#1E2228",
    surface="#242930",
    panel="#2D333B",
    dark=True,
    variables={
        "text-muted": "#7D8794",
        "input-selection-background": "#6FA3D8 30%",
        "block-cursor-background": "#6FA3D8",
        "block-cursor-foreground": "#1E2228",
        "scrollbar": "#2D333B",
        "scrollbar-hover": "#3A414B",
        "scrollbar-active": "#6FA3D8",
        "scrollbar-background": "#1E2228",
        "markdown-h1-color": "#8DB6E2",
        "markdown-h2-color": "#8DB6E2",
        "markdown-h3-color": "#C5CDD8",
        "link-color": "#8DB6E2",
    },
)

BUGMIND_LIGHT_THEME = Theme(
    name="bugmind-light",
    primary="#3D6FA5",
    secondary="#3F8170",
    accent="#6E5FA8",
    warning="#9A6B16",
    error="#B4454E",
    success="#3D7F49",
    foreground="#2B3440",
    background="#F4F5F7",
    surface="#FBFBFC",
    panel="#E6E9ED",
    dark=False,
    variables={
        "text-muted": "#66707D",
        "input-selection-background": "#3D6FA5 25%",
        "markdown-h1-color": "#3D6FA5",
        "markdown-h2-color": "#3D6FA5",
        "markdown-h3-color": "#2B3440",
        "link-color": "#3D6FA5",
    },
)

NAV = [
    ("WORKSPACE", [("new_chat", "✦", "New Chat"), ("analysis", "◎", "Bug Analysis"), ("history", "≡", "Bug History")]),
    ("KNOWLEDGE", [("rules", "◆", "Prevention Rules"), ("patterns", "◈", "Bug Patterns"), ("stats", "▦", "Statistics")]),
    ("CONFIGURATION", [("providers", "◉", "AI Providers"), ("templates", "▤", "Templates"), ("settings", "✱", "Settings")]),
]
VIEW_FOR = {"new_chat": "chat", "analysis": "chat", "history": "history", "rules": "rules",
            "patterns": "patterns", "stats": "stats", "providers": "providers", "templates": "templates",
            "settings": "settings"}

KEYS_HELP = """# BugMind — keyboard shortcuts

| Key | Action |
|---|---|
| `/` | Open the command menu (↑/↓ choose · Enter run · Tab complete · Esc close) |
| `Enter` | Send message |
| `↑` / `↓` | Previous / next sent message (on the first / last line of the input) |
| `Shift+Enter` / `Ctrl+J` / `Alt+Enter` | New line (Shift+Enter needs a terminal that reports it) |
| `Ctrl+P` | Select AI provider |
| `Ctrl+N` | New chat |
| `Ctrl+B` | Bug Analysis mode |
| `Ctrl+H` / `F3` | Bug history |
| `Ctrl+V` | Paste: an image / copied image file on the clipboard is attached, text is inserted |
| `Ctrl+L` | Clear the input box |
| `F2` | Focus the sidebar |
| `Ctrl+S` | Save the template being edited (Templates screen) |
| `Esc` | Cancel running analysis / focus the composer |
| `F1` / `?` | This help |
| `Ctrl+Q` | Quit |

In **Bug History**: `c` confirm · `x` mark invalid · `u` unconfirm the highlighted bug.
Bug reports in the chat have **Save · Discard · Copy report** buttons; saved bugs get **Copy · Open**.
Click a bug under **Recent bugs** to open it: `y` copy · `c` confirm · `x` invalid · `Esc` close.

""" + HELP_TEXT


class ProviderSelectScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, current: str | None, required: bool = False) -> None:
        super().__init__()
        self.current, self.required = current, required

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Static("[b $primary]◉ Select your AI provider[/]", classes="modal-title")
            yield OptionList(*[
                Option(f"{i}. {PROVIDER_LABELS[n]}" + ("  (current)" if n == self.current else "")
                       + ("  — configure the command under AI Providers" if n == "custom" else ""), id=n)
                for i, n in enumerate(PROVIDER_NAMES, 1)
            ], id="provider-options")
            yield Static("[$text-muted]↑/↓ then Enter  ·  1-3  ·  Esc to cancel[/]")

    def on_key(self, event) -> None:
        if event.key in ("1", "2", "3"):
            self.dismiss(PROVIDER_NAMES[int(event.key) - 1])

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q,question_mark", "dismiss(None)", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal", classes="help"):
            yield Markdown(KEYS_HELP)


class BugMindApp(App):
    TITLE = "BugMind"
    CSS_PATH = Path(__file__).with_name("app.tcss")
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+p", "select_provider", "Provider", priority=True),
        Binding("ctrl+n", "new_chat", "New Chat", priority=True),
        Binding("ctrl+b", "bug_mode", "Bug Mode", show=False, priority=True),
        Binding("ctrl+h,f3", "show('history')", "History", priority=True),
        Binding("ctrl+l", "clear_input", "Clear", show=False, priority=True),
        Binding("f2", "focus_sidebar", "Sidebar", show=False, priority=True),
        Binding("f1", "help", "Help", priority=True),
        Binding("question_mark", "help", "Help", show=False),
        Binding("escape", "escape", "Cancel/Focus", show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, cfg: AppConfig | None = None) -> None:
        super().__init__()
        self.cfg = cfg or load_config()
        self.service = BugMindService(self.cfg)
        self.status = "ready" if self.cfg.provider.default else "none"
        self._bugs: int | None = None

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="body"):
            yield Rail(id="rail")
            with Vertical(id="sidebar-pane"):
                options: list[Option | None] = []
                for section, items in NAV:
                    options.append(Option(f"[$text-muted]{section}[/]", id=f"hdr-{section}", disabled=True))
                    options += [Option(f" {icon}  {label}", id=key) for key, icon, label in items]
                    options.append(None)
                yield OptionList(*options[:-1], id="sidebar")
                yield Static("RECENT BUGS", id="recent-title")
                yield OptionList(id="recent")
                yield NewChatButton("+  New chat", id="sidebar-new")
            with ContentSwitcher(initial="chat", id="main"):
                yield ChatView(id="chat")
                yield HistoryView(id="history")
                yield MemoryView("BACKEND_PREVENTION.md", "Prevention Rules",
                                 "actionable instructions for backend developers & coding agents", id="rules")
                yield MemoryView("BUG_PATTERNS.md", "Bug Patterns", "recurring defect classes", id="patterns")
                yield MemoryView("STATISTICS.md", "Statistics", "derived from the archive", id="stats")
                yield ProvidersView(id="providers")
                yield TemplatesView(id="templates")
                yield SettingsView(id="settings")
            yield ContextPanel(id="context")
        yield StatusBar(id="statusbar")

    def on_mount(self) -> None:
        self.register_theme(CRIMSON_THEME)
        self.register_theme(BUGMIND_THEME)
        self.register_theme(SLATE_THEME)
        self.register_theme(BUGMIND_LIGHT_THEME)
        self.theme = self.cfg.tui.theme if self.cfg.tui.theme in self.available_themes else "bugmind-crimson"
        self.query_one("#main").border_title = "Chat"
        self._layout(self.size.width)
        self.knowledge_changed()
        self._select_nav("new_chat")
        self.query_one(Composer).focus()
        if not self.ensure_provider(startup=True) and self.cfg.provider.default is None:
            self.call_after_refresh(self.action_select_provider)

    def ensure_provider(self, startup: bool = False) -> bool:
        """Make sure the provider shown is one that can actually run. If the configured one can't
        (not set, custom without a command, CLI uninstalled), switch to an installed agent CLI."""
        cfg = self.service.cfg
        name = auto_provider(cfg)
        if name is None:
            if not startup:
                self.notify("No AI agent found. Install and log in to Claude Code (`claude`) or OpenCode "
                            "(`opencode`), or configure a custom agent under AI Providers.",
                            title="No provider", severity="error", timeout=10)
            self.set_status("none" if cfg.provider.default is None else "error")
            return False
        if name != cfg.provider.default:
            old = PROVIDER_LABELS.get(cfg.provider.default or "", "")
            if not self.apply_config(set_value(cfg, "provider.default", name)):
                return False
            reason = f"{old} isn't set up on this machine" if old else "detected on this machine"
            self.notify(f"Using {self.provider_label()} — {reason}.", title="AI provider")
        return True

    # ------------------------------------------------------------- shared helpers
    def provider_label(self) -> str:
        name = self.service.cfg.provider.default
        if name == "custom":
            return self.service.cfg.provider.custom.name
        return PROVIDER_LABELS.get(name or "", "none")

    def set_status(self, status: str) -> None:
        self.status = status if self.service.cfg.provider.default else "none"
        self.refresh_header()

    @property
    def chat(self) -> ChatView:
        return self.query_one(ChatView)

    def refresh_header(self) -> None:
        """Cheap refresh of everything that shows provider / status / mode / draft."""
        self.query_one(TopBar).refresh_info(self)
        self.query_one(StatusBar).refresh_info(self, self._bugs)
        self.query_one(ContextPanel).update_status(self)
        main = self.query_one("#main")
        title = "Chat" if main.current == "chat" else ""  # other views carry their own title row
        main.border_subtitle = "[$warning]● analyzing[/]" if self.status == "working" else ""
        main.border_title = title

    # ----------------------------------------------------------------- copy on select
    _last_selection = ""

    def _copy_selection(self, text: str | None) -> None:
        if text and text.strip():
            copy_text(self, text, "Selection", timeout=1.5)

    def on_text_selected(self, event: events.TextSelected) -> None:
        """Mouse selection on any read-only text (messages, reports, tables) copies it at once."""
        self._copy_selection(self.screen.get_selected_text())

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """TextArea (composer, template editor) keeps its own selection and never sends TextSelected."""
        w = self.focused
        text = w.selected_text if isinstance(w, TextArea) else ""
        if text != self._last_selection:  # a later click on a button must not re-copy a stale selection
            self._last_selection = text
            self._copy_selection(text)

    def on_resize(self, event) -> None:
        self._layout(event.size.width)

    def _layout(self, width: int) -> None:
        # shortcut: simple width thresholds instead of a breakpoint system
        self.query_one("#context").display = width >= 130
        self.query_one("#sidebar-pane").display = width >= 96

    def apply_config(self, cfg: AppConfig) -> bool:
        """Persist and switch to *cfg*. Refused while an analysis runs: the worker holds the old
        service, so a provider/project switch would save the result into the wrong place."""
        if self.chat.busy:
            self.notify("An analysis is running — wait for it or press Esc to cancel, then try again.",
                        severity="warning")
            return False
        try:
            save_config(cfg)
        except OSError as exc:
            self.notify(f"Could not save settings: {exc}", severity="error")
            return False
        self.cfg = cfg
        self.service = BugMindService(cfg)  # storage/project may have changed
        self.set_status("ready")
        self.knowledge_changed()
        return True

    def notify(self, message: str, **kw) -> None:  # type: ignore[override]
        # Toasts often carry exception text / ids / paths: never parse them as markup.
        kw.setdefault("markup", False)
        super().notify(message, **kw)

    def knowledge_changed(self) -> None:
        try:
            k = self.service.knowledge()
        except Exception:
            k = None
        self._bugs = len(k.records) if k else None
        self.query_one(ContextPanel).update_knowledge(k)
        recent = self.query_one("#recent", OptionList)
        recent.clear_options()
        if k:
            recent.add_options(recent_options(k.records))
        self.refresh_header()
        current = self.query_one(ContentSwitcher).current
        if current and current != "chat":
            self.query_one(f"#{current}").reload()  # type: ignore[attr-defined]

    def _select_nav(self, key: str) -> None:
        sidebar = self.query_one("#sidebar", OptionList)
        try:
            index = sidebar.get_option_index(key)
        except Exception:
            return
        with sidebar.prevent(OptionList.OptionHighlighted):  # sync highlight without re-navigating
            sidebar.highlighted = index

    # ------------------------------------------------------------- navigation
    @on(OptionList.OptionSelected, "#sidebar")
    @on(OptionList.OptionHighlighted, "#sidebar")
    def _nav(self, event: OptionList.OptionSelected | OptionList.OptionHighlighted) -> None:
        key = str(event.option.id)
        if key not in VIEW_FOR:
            return
        chat = self.query_one(ChatView)
        if isinstance(event, OptionList.OptionSelected):
            if key == "new_chat":
                chat.clear()
                chat.mode = "chat"
            elif key == "analysis":
                chat.mode = "bug"
        self.action_show(VIEW_FOR[key], focus=isinstance(event, OptionList.OptionSelected))

    @on(OptionList.OptionSelected, "#recent")
    def _open_recent(self, event: OptionList.OptionSelected) -> None:
        self.open_bug(str(event.option.id))

    def open_bug(self, bug_id: str) -> None:
        self.push_screen(BugDetailScreen(bug_id))

    def navigate(self, key: str) -> None:
        if key == "new_chat":
            self.action_new_chat()
        else:
            self.action_show(VIEW_FOR[key])

    def action_show(self, view: str, focus: bool = True) -> None:
        switcher = self.query_one(ContentSwitcher)
        switcher.current = view
        self.query_one(Rail).set_active("new_chat" if view == "chat" else view)
        self.refresh_header()
        widget = self.query_one(f"#{view}")
        if hasattr(widget, "reload"):
            try:
                widget.reload()
            except Exception as exc:
                self.notify(f"Could not load {view}: {exc}", severity="error")
        if view != "chat":
            self._select_nav(view)
        if focus:
            target = {"chat": Composer, "history": "#history-table", "templates": "#templates-list"}.get(view)
            if target is not None:
                self.query_one(target).focus()  # type: ignore[arg-type]
            else:
                widget.focus()

    # ------------------------------------------------------------- actions
    def set_provider(self, name: str) -> None:
        try:
            cfg = set_value(self.service.cfg, "provider.default", name)
        except ConfigError as exc:
            self.notify(str(exc), severity="error")
            return
        if not usable(cfg, name):  # never show a provider that would fail on the next message
            what = ("Set the custom agent command first (AI Providers → Custom agent command)."
                    if name == "custom" else
                    f"`{getattr(cfg.provider, name).executable}` was not found on PATH. Install it or set its path.")
            self.notify(what, title=f"{PROVIDER_LABELS[name]} is not available", severity="error", timeout=8)
            if name == "custom":
                self.action_show("providers")
            return
        if not self.apply_config(cfg):
            return
        self.notify(f"AI provider: {self.provider_label()}", title="Provider switched")

    def action_select_provider(self) -> None:
        def chosen(name: str | None) -> None:
            if name:
                self.set_provider(name)

        self.push_screen(ProviderSelectScreen(self.service.cfg.provider.default), chosen)

    def action_new_chat(self) -> None:
        chat = self.query_one(ChatView)
        chat.clear()
        chat.mode = "chat"
        self._select_nav("new_chat")
        self.action_show("chat")

    def action_bug_mode(self) -> None:
        self.query_one(ChatView).mode = "bug"
        self._select_nav("analysis")
        self.action_show("chat")

    def action_clear_input(self) -> None:
        self.query_one(Composer).clear()

    def action_focus_sidebar(self) -> None:
        self.query_one("#sidebar").focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_escape(self) -> None:
        if not self.query_one(ChatView).cancel():
            self.action_show("chat")


def run_tui() -> None:
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise SystemExit(f"Config error: {exc}")
    BugMindApp(cfg).run()
