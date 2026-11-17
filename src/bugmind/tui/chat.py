"""Chat workspace: welcome card, message list, live streaming area, slash-command menu, composer."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, LoadingIndicator, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from bugmind.analysis import ResponseParseError
from bugmind.config import PROVIDER_LABELS, PROVIDER_NAMES, ConfigError, set_value
from bugmind.images import MAX_IMAGES, Attachment, ImageError, extract_images, load_image, read_clipboard
from bugmind.models import Intent
from bugmind.providers import ProviderError
from bugmind.security import escape_markup as escape
from bugmind.security import strip_control_chars
from bugmind.service import DuplicateBugError, Draft


# ----------------------------------------------------------------------------- command registry


@dataclass(frozen=True)
class Command:
    name: str  # what gets typed; a trailing space means "needs an argument"
    description: str
    group: str


STATIC_COMMANDS: list[Command] = [
    Command("/provider", "Choose the AI provider", "Provider"),
    *(Command(f"/provider {n}", f"Switch to {PROVIDER_LABELS[n]}", "Provider") for n in PROVIDER_NAMES),
    Command("/mode bug", "Analyze every message as a bug", "Chat"),
    Command("/mode chat", "Detect bug reports automatically", "Chat"),
    Command("/new", "Start a new chat", "Chat"),
    Command("/clear", "Clear the conversation", "Chat"),
    Command("/save", "Save the pending draft", "Draft"),
    Command("/save!", "Save the draft even if it is a duplicate", "Draft"),
    Command("/discard", "Discard the pending draft", "Draft"),
    Command("/attach ", "Attach a screenshot — /attach <path> (or drag the image into the terminal)", "Draft"),
    Command("/detach", "Remove the attached screenshots", "Draft"),
    Command("/copy", "Copy the latest report to the clipboard", "Draft"),
    Command("/history", "Browse saved bugs", "Navigate"),
    Command("/search ", "Search bugs — /search <terms>", "Navigate"),
    Command("/rules", "Backend prevention rules", "Navigate"),
    Command("/patterns", "Recurring bug patterns", "Navigate"),
    Command("/stats", "Statistics", "Navigate"),
    Command("/providers", "Provider settings", "Navigate"),
    Command("/templates", "Report templates", "Navigate"),
    Command("/settings", "Application settings", "Navigate"),
    Command("/export prevention", "Save the bug-prevention instructions (BACKEND_PREVENTION.md) to Downloads", "Knowledge"),
    Command("/export all", "Save the full knowledge package (.zip) to Downloads", "Knowledge"),
    Command("/export patterns", "Save BUG_PATTERNS.md to Downloads", "Knowledge"),
    Command("/export stats", "Save STATISTICS.md to Downloads", "Knowledge"),
    Command("/rebuild", "Rebuild memory from the bug archive", "Knowledge"),
    Command("/help", "Commands and shortcuts", "App"),
    Command("/quit", "Quit BugMind", "App"),
]

MAX_MESSAGES = 200


def match_commands(query: str, commands: list[Command], limit: int = 9) -> list[Command]:
    """Prefix matches; if there are none, substring matches on name or description."""
    q = query.lower()
    prefix = [c for c in commands if c.name.startswith(q)]
    if prefix:
        return prefix[:limit]
    bare = q.lstrip("/").strip()
    return [c for c in commands if bare and (bare in c.name or bare in c.description.lower())][:limit]


COMMAND_WORDS = {c.name.split()[0] for c in STATIC_COMMANDS} | {"/template"}


def is_command(text: str) -> bool:
    """Slash commands vs. bug text that merely starts with a path ("/api/v1/orders returns 500")."""
    if not text.startswith("/"):
        return False
    first = text.split(maxsplit=1)[0].lower()
    if first in COMMAND_WORDS:
        return True
    return len(text.split()) == 1 and first.count("/") == 1  # a lone "/typo" still reports "Unknown command"


HELP_TEXT = "**Chat commands** — type `/` to open the command menu\n\n| Command | Action |\n|---|---|\n" + "\n".join(
    f"| `{c.name.strip()}` | {c.description} |" for c in STATIC_COMMANDS
) + """
| `/template <id>` | Use another report template |

While a draft has open questions, simply reply with the extra details: the agent merges them into
the same report. Reports are always written in English; you can write in Arabic or English.
"""


# ----------------------------------------------------------------------------- widgets


class Composer(TextArea):
    """Multiline input: Enter sends, Shift+Enter / Ctrl+J / Alt+Enter insert a newline.

    While the slash-command menu is open, ↑/↓/Tab/Enter/Esc drive the menu.
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    # Windows Terminal keeps Ctrl+V for its own text paste, so screenshots need another key there.
    BINDINGS = [Binding("alt+v", "paste", "Paste image", show=False)]

    chat: ChatView | None = None

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.sent: list[str] = []
        self._hpos = 0

    def remember(self, text: str) -> None:
        if not self.sent or self.sent[-1] != text:
            self.sent = (self.sent + [text])[-100:]
        self._hpos = len(self.sent)

    def _recall(self, step: int) -> bool:
        """↑ on the first line / ↓ on the last line walk through earlier messages."""
        row = self.cursor_location[0]
        if (step < 0 and row != 0) or (step > 0 and row != self.document.line_count - 1) or not self.sent:
            return False
        pos = self._hpos + step
        if pos < 0 or pos > len(self.sent):
            return False
        self._hpos = pos
        self.text = self.sent[pos] if pos < len(self.sent) else ""
        self.move_cursor(self.document.end)
        return True

    def action_paste(self) -> None:
        """Ctrl+V like Claude Code / OpenCode: an image (or a copied image file) on the system clipboard
        is attached; text is pasted. Read in a thread so a slow clipboard never freezes the UI."""
        self.run_worker(self._read_clipboard, thread=True, group="clipboard", exclusive=True)

    def _read_clipboard(self) -> None:
        try:
            images, text = read_clipboard()
        except (ImageError, OSError) as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return
        self.app.call_from_thread(self._pasted, images, text)

    def _pasted(self, images: list[Attachment], text: str | None) -> None:
        if images and self.chat:
            self.chat.attach(images)
        elif text:
            self.insert(text)
        else:
            self.app.notify("The clipboard is empty (on Linux, install wl-clipboard or xclip).", severity="warning")

    async def _on_paste(self, event: events.Paste) -> None:
        # Terminal paste (Ctrl+Shift+V, drag & drop) of nothing but image paths -> attach right away.
        if self.chat:
            rest, found = extract_images(event.text)
            if found and not rest:
                event.stop()
                event.prevent_default()
                self.chat.attach(found)
                return
        await super()._on_paste(event)

    async def _on_key(self, event: events.Key) -> None:
        if self.chat and self.chat.menu_open and event.key in ("up", "down", "tab", "enter", "escape"):
            event.stop()
            event.prevent_default()
            self.chat.menu_key(event.key)
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.text.strip():
                self.remember(self.text.strip())
                self.post_message(self.Submitted(self.text))
            return
        if event.key in ("up", "down") and self._recall(-1 if event.key == "up" else 1):
            event.stop()
            event.prevent_default()
            return
        if event.key in ("shift+enter", "ctrl+j", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)


class Hero(Vertical):
    """Welcome card with live archive numbers."""

    def compose(self) -> ComposeResult:
        svc = self.app.service  # type: ignore[attr-defined]
        try:
            k = svc.knowledge()
            nums = [(len(k.records), "bugs"), (len(k.patterns), "patterns"),
                    (len(k.active_rules), "active rules"), (len(k.candidate_rules), "candidates")]
        except Exception:
            nums = [("–", "bugs"), ("–", "patterns"), ("–", "active rules"), ("–", "candidates")]
        yield Static("[b $primary]◆ BugMind[/]  [$text-muted]AI bug intelligence[/]", classes="hero-title")
        yield Static("Turn bug descriptions — Arabic or English — into structured English QA reports, "
                     "recurring patterns and backend prevention rules.", classes="hero-sub")
        with Horizontal(classes="hero-stats"):
            for value, label in nums:
                yield Static(f"[b]{value}[/]\n[$text-muted]{label}[/]", classes="stat-card")
        yield Static("[$text-muted]Paste a bug or drag a screenshot in   ·   [/][b $primary]/[/][$text-muted] commands   ·   "
                     "[/][b $primary]Ctrl+P[/][$text-muted] provider   ·   [/][b $primary]F1[/][$text-muted] help[/]",
                     classes="hero-tips")


class ChatMessage(Vertical):
    LABELS = {"user": ("❯", "You"), "assistant": ("●", "BugMind"), "error": ("✕", "Error"), "system": ("✓", "BugMind")}

    def __init__(self, role: str, body: str, *, markdown: bool = True, meta: str = "",
                 actions: list[tuple[str, str, str]] = (), payload: dict | None = None) -> None:
        super().__init__(classes=f"msg msg-{role}")
        self.actions, self.payload = list(actions), payload or {}
        self.role = role
        self.body = strip_control_chars(body)
        self.markdown = markdown
        self.meta = meta

    def compose(self) -> ComposeResult:
        icon, label = self.LABELS.get(self.role, ("·", self.role))
        stamp = datetime.now().strftime("%H:%M")
        meta = f"  [$text-muted]·  {escape(self.meta)}[/]" if self.meta else ""
        yield Static(f"[b]{icon} {label}[/]  [$text-muted]{stamp}[/]{meta}", classes="msg-head")
        if self.markdown:
            yield Markdown(self.body, classes="msg-body", open_links=False)  # AI text: no link launching
        else:
            yield Static(self.body, classes="msg-body", markup=False)
        if self.actions:
            with Horizontal(classes="msg-actions"):
                for name, label, variant in self.actions:
                    yield Button(label, name=name, variant=variant, compact=True, classes=f"msg-action act-{name}")

    def drop_actions(self, *names: str) -> None:
        self.actions = [a for a in self.actions if a[0] not in names]  # not composed yet (auto-save)
        for name in names:
            for b in self.query(f".act-{name}"):
                b.remove()


# ----------------------------------------------------------------------------- chat view


class ChatView(Vertical):
    mode: reactive[str] = reactive("chat")  # "chat" (intent detection) | "bug" (force analysis)
    busy: reactive[bool] = reactive(False)

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.draft: Draft | None = None
        self._received = 0
        self._tail = ""
        self._started = 0.0
        self._last_paint = 0.0
        self._menu_items: list[Command] = []
        self._draft_msg: ChatMessage | None = None
        self.pending: list[Attachment] = []  # screenshots waiting for the next message
        self._last_report = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="chat-title"):
            yield Static("", id="chat-mode")
            yield Static("", id="chat-draft")
            yield Static(f"Commands [b]{len(STATIC_COMMANDS)}[/] ▾", id="cmd-chip")
        yield VerticalScroll(id="messages")
        with Horizontal(id="live"):
            yield LoadingIndicator(id="spinner")
            yield Static("", id="live-text")
        yield OptionList(id="cmd-menu")
        yield Static("", id="attach-bar")
        with Horizontal(id="composer-row"):
            yield Static("❯", id="prompt")
            yield Composer(id="composer", soft_wrap=True, show_line_numbers=False,
                           placeholder="Describe a bug, paste logs, or type / for commands…")
        yield Static("", id="composer-hint")

    def on_click(self, event: events.Click) -> None:
        if event.widget is not None and event.widget.id == "cmd-chip":
            composer = self.query_one(Composer)
            composer.focus()
            if not composer.text:
                composer.insert("/")

    def on_mount(self) -> None:
        self.query_one("#attach-bar").display = False
        self.query_one("#live").display = False
        self.query_one("#cmd-menu").display = False
        composer = self.query_one(Composer)
        composer.chat = self
        composer.border_title = "Message"
        self.welcome()
        self._refresh_titles()
        self.call_after_refresh(self._update_hint)  # is_mounted is still False inside on_mount

    # -------------------------------------------------------------- helpers
    def welcome(self) -> None:
        # No fixed id: remove_children() completes asynchronously, so a fast second clear()
        # (e.g. double-clicking "New Chat") would otherwise mount a duplicate id and crash.
        self.query_one("#messages", VerticalScroll).mount(Hero(classes="hero"))

    def add(self, role: str, body: str, *, markdown: bool = True, meta: str = "",
            actions: list[tuple[str, str, str]] = (), payload: dict | None = None) -> ChatMessage:
        box = self.query_one("#messages", VerticalScroll)
        box.query(".hero").remove()  # the welcome card gives way once the conversation starts
        msg = ChatMessage(role, body, markdown=markdown, meta=meta, actions=actions, payload=payload)
        box.mount(msg)
        old = box.query(ChatMessage)
        for stale in list(old)[: max(0, len(old) - MAX_MESSAGES)]:  # keep long sessions responsive
            if stale is not self._draft_msg:
                stale.remove()
        box.call_after_refresh(box.scroll_end, animate=False)
        return msg

    def _close_draft(self) -> None:
        """The draft was saved, discarded or superseded: its Save/Discard buttons are stale."""
        if self._draft_msg is not None:
            self._draft_msg.drop_actions("save", "save-force", "discard")
        self._draft_msg = None
        self.draft = None
        self._refresh_titles()

    def copy_report(self, text: str = "") -> None:
        text = text or self._last_report
        if not text:
            self.add("error", "No report to copy yet.")
            return
        from bugmind.tui.panels import copy_text
        copy_text(self.app, text)

    @on(Button.Pressed, ".msg-action")
    def _message_action(self, event: Button.Pressed) -> None:
        event.stop()
        msg = next((a for a in event.button.ancestors if isinstance(a, ChatMessage)), None)
        payload = msg.payload if msg else {}
        name = event.button.name
        if name in ("save", "save-force"):
            self.save(force=name == "save-force")
        elif name == "discard":
            self.command("/discard")
        elif name == "copy":
            self.copy_report(payload.get("report", ""))
        elif name == "open" and payload.get("bug_id"):
            self.app.open_bug(payload["bug_id"])  # type: ignore[attr-defined]
        self.query_one(Composer).focus()

    def attach(self, images: list[Attachment]) -> None:
        for img in images:
            if any(p.path == img.path or p.digest() == img.digest() for p in self.pending):
                self.app.notify(f"{img.display_name} is already attached.")
                continue
            if len(self.pending) >= MAX_IMAGES:
                self.add("error", f"At most {MAX_IMAGES} screenshots per bug.")
                break
            self.pending.append(img)
        self._refresh_attach()

    def _refresh_attach(self) -> None:
        bar = self.query_one("#attach-bar", Static)
        bar.display = bool(self.pending)
        chips = "   ".join(f"[b $primary]▣ {escape(f"[Image #{n}]")}[/] [$text-muted]{escape(i.label())}[/]"
                           for n, i in enumerate(self.pending, 1))
        bar.update(f"{chips}   [$text-muted]· sent with your next message · /detach to remove[/]")

    def clear(self) -> None:
        if self.busy:  # a running analysis must not land (and auto-save) in the new chat
            self.workers.cancel_group(self, "ai")
            self.busy = False
        self.query_one("#messages", VerticalScroll).remove_children()
        self.pending = []
        self._refresh_attach()
        self._draft_msg = None
        self.draft = None
        self._refresh_titles()
        self.welcome()

    def watch_mode(self) -> None:
        if self.is_mounted:
            self._refresh_titles()

    def _update_hint(self) -> None:
        self.query_one("#live").display = self.busy
        hint = ("[b $warning]Esc[/] [$text-muted]cancel[/]" if self.busy else
                "[b]Enter[/] [$text-muted]send[/]   [b]Shift+Enter[/] [$text-muted]newline[/]   "
                "[b]Ctrl+V[/] [$text-muted]paste image[/]   [b]/[/] [$text-muted]commands[/]   "
                "[b]Ctrl+L[/] [$text-muted]clear[/]")
        self.query_one("#composer-hint", Static).update(hint)

    def watch_busy(self, busy: bool) -> None:
        if not self.is_mounted:
            return
        try:
            self._update_hint()
        except NoMatches:  # app shutting down while a cancelled worker unwinds
            return
        self.app.set_status("working" if busy else "ready")  # type: ignore[attr-defined]

    def _refresh_titles(self) -> None:
        mode = ("[$warning]●[/] bug-analysis  [$text-muted]every message is a bug[/]" if self.mode == "bug"
                else "[$success]●[/] chat  [$text-muted]bugs detected automatically[/]")
        self.query_one("#chat-mode", Static).update(mode)
        self.query_one("#chat-draft", Static).update(
            "[b $warning]● draft pending[/]  [$text-muted]/save · /discard[/]" if self.draft else "")
        if self.is_attached and hasattr(self.app, "refresh_header"):
            self.app.refresh_header()  # type: ignore[attr-defined]

    # -------------------------------------------------------------- slash-command menu
    @property
    def menu_open(self) -> bool:
        return bool(self.query_one("#cmd-menu").display)

    def _commands(self) -> list[Command]:
        extra = []
        try:
            for tid, lang, _ in self.app.service.engine.list():  # type: ignore[attr-defined]
                if lang in ("en", "any"):
                    extra.append(Command(f"/template {tid}", "Use this report template", "Template"))
        except Exception:
            pass
        return STATIC_COMMANDS + extra

    @on(TextArea.Changed, "#composer")
    def _composer_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        menu = self.query_one("#cmd-menu", OptionList)
        if not text.startswith("/") or "\n" in text:
            menu.display = False
            return
        self._menu_items = match_commands(text, self._commands())
        if not self._menu_items:
            menu.display = False
            return
        menu.clear_options()
        width = max(len(c.name) for c in self._menu_items) + 3
        dwidth = max(len(c.description) for c in self._menu_items) + 3
        for c in self._menu_items:
            menu.add_option(Option(f"[b $primary]{escape(c.name.strip()):<{width}}[/]"
                                   f"{escape(c.description):<{dwidth}}[$text-muted]{c.group}[/]", id=c.name))
        menu.highlighted = 0
        menu.display = True

    def menu_key(self, key: str) -> None:
        menu = self.query_one("#cmd-menu", OptionList)
        composer = self.query_one(Composer)
        if key == "escape":
            menu.display = False
        elif key == "up":
            menu.action_cursor_up()
        elif key == "down":
            menu.action_cursor_down()
        elif menu.highlighted is not None:
            cmd = self._menu_items[menu.highlighted]
            if key == "tab" or cmd.name.endswith(" "):
                composer.text = cmd.name
                composer.move_cursor(composer.document.end)
            else:
                self.run_command(cmd.name)

    @on(OptionList.OptionSelected, "#cmd-menu")
    def _menu_clicked(self, event: OptionList.OptionSelected) -> None:
        name = str(event.option.id)
        if name.endswith(" "):
            composer = self.query_one(Composer)
            composer.text = name
            composer.move_cursor(composer.document.end)
            composer.focus()
        else:
            self.run_command(name)

    def run_command(self, text: str) -> None:
        self.query_one("#cmd-menu").display = False
        self.query_one(Composer).clear()
        self.query_one(Composer).focus()
        self.command(text)

    # -------------------------------------------------------------- input
    @on(Composer.Submitted)
    def submitted(self, event: Composer.Submitted) -> None:
        if self.busy:
            self.app.notify("Still working on the previous message (Esc cancels).", severity="warning")
            return
        text = event.text.strip()
        self.query_one(Composer).clear()
        if is_command(text):
            self.command(text)
            return
        text, found = extract_images(text)  # drag & drop pastes the file path
        if found:
            self.attach(found)
            if not text:
                self.add("system", f"Attached {', '.join(f'`{i.name}`' for i in found)} — now describe the bug.")
                return
        if not self.app.ensure_provider():  # type: ignore[attr-defined]
            self.attach(found)
            self.query_one(Composer).insert(text)  # keep what the user wrote
            return
        images, self.pending = self.pending, []
        self._refresh_attach()
        meta = "▣ " + ", ".join(i.display_name for i in images) if images else ""
        self.add("user", text, markdown=False, meta=meta)
        self.process(text, images)

    def command(self, text: str) -> None:
        app = self.app
        svc = app.service  # type: ignore[attr-defined]
        cmd, _, arg = text.strip().partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        views = {"/history": "history", "/rules": "rules", "/patterns": "patterns", "/stats": "stats",
                 "/providers": "providers", "/templates": "templates", "/settings": "settings"}
        if cmd in ("/save", "/save!"):
            self.save(force=cmd == "/save!")
        elif cmd == "/discard":
            if not self.draft:
                self.add("error", "No draft to discard.")
                return
            self._close_draft()
            self.add("system", "Draft discarded.")
        elif cmd == "/attach":
            if not arg:
                self.add("error", "Usage: `/attach <path-to-image>` — or drag the image file into the terminal.")
                return
            try:
                self.attach([load_image(arg.strip("'\""))])
            except ImageError as exc:
                self.add("error", str(exc))
        elif cmd == "/detach":
            self.pending = []
            self._refresh_attach()
            self.add("system", "Screenshots removed.")
        elif cmd == "/copy":
            self.copy_report()
        elif cmd == "/new":
            app.action_new_chat()  # type: ignore[attr-defined]
        elif cmd == "/clear":
            self.clear()
        elif cmd == "/mode":
            self.mode = "bug" if arg.lower().startswith("bug") else "chat"
            self.add("system", f"Mode: **{'Bug Analysis' if self.mode == 'bug' else 'Chat'}**")
        elif cmd == "/provider":
            if not arg:
                app.action_select_provider()  # type: ignore[attr-defined]
            elif arg.lower() in PROVIDER_NAMES:
                app.set_provider(arg.lower())  # type: ignore[attr-defined]
            else:
                self.add("error", f"Unknown provider `{arg}`. Use one of: {', '.join(PROVIDER_NAMES)}.")
        elif cmd in views:
            app.action_show(views[cmd])  # type: ignore[attr-defined]
        elif cmd == "/search":
            app.action_show("history")  # type: ignore[attr-defined]
            from textual.widgets import Input
            app.query_one("#history-search", Input).value = arg
        elif cmd == "/template":
            try:
                svc.engine.resolve(arg)
                if app.apply_config(set_value(svc.cfg, "output.template", arg)):  # type: ignore[attr-defined]
                    self.add("system", f"Report template: `{arg}`")
            except (ConfigError, ValueError, Exception) as exc:
                self.add("error", f"Cannot use template `{arg}`: {exc}")
        elif cmd == "/export":
            from bugmind.export import ExportError, downloads_dir, export_knowledge
            what = arg or "prevention"
            try:
                path = export_knowledge(svc, what, downloads_dir(), as_zip=what == "all", unique=True)
                self.add("system", f"✓ Saved to Downloads: `{path}`")
            except (ExportError, OSError) as exc:
                self.add("error", f"Export failed: {exc}")
        elif cmd == "/rebuild":
            try:
                files = svc.rebuild_memory()
            except Exception as exc:  # storage errors must be visible, never crash the app
                self.add("error", f"Rebuild failed: {exc}")
                return
            self.add("system", f"✓ Rebuilt {len(files)} memory files from {len(svc.store)} archived bug(s).")
            app.knowledge_changed()  # type: ignore[attr-defined]
        elif cmd == "/help":
            self.add("system", HELP_TEXT)
        elif cmd == "/quit":
            app.exit()
        else:
            self.add("error", f"Unknown command `{cmd}`. Type `/` to see all commands.")

    def cancel(self) -> bool:
        if self.menu_open:
            self.query_one("#cmd-menu").display = False
            return True
        if self.busy:
            self.workers.cancel_group(self, "ai")
            self.busy = False
            self.add("error", "Cancelled. The agent process was terminated.")
            return True
        return False

    # -------------------------------------------------------------- AI work
    def _on_chunk(self, chunk: str) -> None:
        self._received += len(chunk)
        self._tail = (self._tail + chunk)[-600:]
        now = time.monotonic()
        if now - self._last_paint < 0.1:
            return
        self._last_paint = now
        tail = strip_control_chars(self._tail).replace("\n", " ")[-170:]
        self.query_one("#live-text", Static).update(
            f"[b $primary]{escape(self.app.provider_label())}[/] [$text-muted]is analyzing[/]   "  # type: ignore[attr-defined]
            f"[b]{self._received:,}[/] [$text-muted]chars ·[/] [b]{now - self._started:.0f}s[/]\n"
            f"[$text-muted]{escape(tail)}[/]")

    @work(exclusive=True, group="ai")
    async def process(self, text: str, images: list[Attachment] | None = None) -> None:
        svc = self.app.service  # type: ignore[attr-defined]
        context = ""
        original = text
        images = list(images or [])
        if self.draft and self.draft.is_bug:
            # Follow-up to a pending draft: merge the extra details (and keep its screenshots).
            original = f"{self.draft.original_input}\n\nAdditional information:\n{text}"
            context = self.draft.report
            images = self.draft.images + [i for i in images if i.path not in {d.path for d in self.draft.images}]
        self.busy = True
        self._received, self._tail, self._started, self._last_paint = 0, "", time.monotonic(), 0.0
        self.query_one("#live-text", Static).update(
            f"[b $primary]{escape(self.app.provider_label())}[/] [$text-muted]is starting…[/]")  # type: ignore[attr-defined]
        try:
            draft = await svc.analyze(original, force_bug=self.mode == "bug" or bool(context), context=context,
                                      on_chunk=self._on_chunk, images=images)
        except DuplicateBugError as exc:
            self.add("error", f"{exc}. Use `/mode chat` and `/save!` if you really want a second copy.")
            return
        except (ProviderError, ResponseParseError, ValueError) as exc:
            self.busy = False
            self.add("error", f"{exc}")
            self.app.set_status("error")  # type: ignore[attr-defined]
            return
        except Exception as exc:  # template/storage/unknown errors must never kill the app or the session
            self.busy = False
            self.add("error", f"Unexpected error ({type(exc).__name__}): {exc}")
            self.app.set_status("error")  # type: ignore[attr-defined]
            return
        finally:
            self.busy = False
        self.present(draft)

    def present(self, draft: Draft) -> None:
        meta = f"{draft.provider} · {draft.duration_s:.0f}s" + (f" · ${draft.cost_usd:.3f}" if draft.cost_usd else "")
        if not draft.is_bug:
            self.add("assistant", draft.response.reply or "_(no reply)_", meta=meta)
            if draft.response.intent == Intent.BUG_REPORT:
                self.add("error", "The agent classified this as a bug but returned no analysis.")
            return
        bug = draft.response.bug
        assert bug is not None
        self._close_draft()  # a follow-up supersedes the previous draft message
        self.draft = draft
        self._last_report = draft.report
        pa = bug.pattern
        target = pa.matched_pattern_id or pa.related_pattern_id or pa.pattern_name
        lines = [draft.report, "---",
                 f"- **Severity:** `{bug.severity.value}`" + (f" — {bug.severity_justification}" if bug.severity_justification else ""),
                 f"- **Category:** `{bug.category}`",
                 f"- **Pattern:** `{pa.relation.value}`" + (f" → {target}" if pa.relation.value != "insufficient" else "")]
        if bug.hypotheses:
            lines += ["", "**Hypotheses (unconfirmed):**", *(f"- {h}" for h in bug.hypotheses)]
        if draft.redactions:
            lines += ["", f"> {draft.redactions} secret value(s) were redacted before analysis."]
        for d in draft.duplicates:
            lines += ["", f"> Possible duplicate of **{d.bug_id}** ({d.score:.0%} similar)."]
        if bug.clarification_questions:
            lines += ["", "**Missing information — reply with details, or `/save` to keep it as `needs_info`:**",
                      *(f"- {q}" for q in bug.clarification_questions)]
        exact_dup = any(d.exact for d in draft.duplicates)
        save_btn = ("save-force", "✓ Save anyway", "warning") if exact_dup else ("save", "✓ Save", "primary")
        self._draft_msg = self.add("assistant", "\n".join(lines), meta=meta, payload={"report": draft.report},
                                   actions=[save_btn, ("discard", "✕ Discard", "default"),
                                            ("copy", "⧉ Copy report", "default")])
        policy = self.app.service.cfg.analysis.auto_save  # type: ignore[attr-defined]
        if not exact_dup and (policy == "always" or (policy == "complete" and not bug.clarification_questions)):
            self.save()
        else:
            self._refresh_titles()

    def save(self, force: bool = False) -> None:
        if not self.draft or not self.draft.is_bug:
            self.add("error", "No bug draft to save.")
            return
        try:
            res = self.app.service.save(self.draft, force=force)  # type: ignore[attr-defined]
        except DuplicateBugError as exc:
            self.add("error", f"{exc}. Use `/save!` to store it anyway.")
            return
        except Exception as exc:  # storage errors must be visible, never swallowed
            self.add("error", f"Saving failed: {exc}")
            return
        self._close_draft()
        rec = res.record
        self._last_report = rec.formatted_report
        pats = ", ".join(rec.pattern_ids) or "unclassified"
        shots = f" · {len(rec.attachments)} screenshot(s) kept" if rec.attachments else ""
        self.add("system", f"✓ Saved **{rec.id}** · patterns: {pats} · status `{rec.status.value}`{shots} · "
                           f"prevention memory updated.",
                 payload={"bug_id": rec.id, "report": rec.formatted_report},
                 actions=[("copy", "⧉ Copy report", "default"), ("open", f"↗ Open {rec.id}", "default")])
        self.app.knowledge_changed()  # type: ignore[attr-defined]
