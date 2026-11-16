"""Workspace chrome: top bar, icon rail, recent-bug list, context panel and status bar."""

from __future__ import annotations

from collections import Counter

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Button, Markdown, Static
from textual.widgets.option_list import Option

from bugmind import __version__
from bugmind.security import escape_markup as escape
from bugmind.security import strip_control_chars

SEV_STYLE = {"critical": "$error", "high": "$warning", "medium": "$secondary", "low": "$success", "unknown": "$text-muted"}
STATUS_DOT = {"ready": "[$success]●[/]", "working": "[$warning]●[/]", "error": "[$error]●[/]", "none": "[$error]●[/]"}
RAIL = [("new_chat", "+", "New chat"), ("history", "≡", "Bug history"), ("rules", "◆", "Prevention rules"),
        ("patterns", "◈", "Bug patterns"), ("stats", "▦", "Statistics"), ("templates", "▤", "Templates"),
        ("providers", "◉", "AI providers")]


def _clip(text: str, n: int) -> str:
    text = strip_control_chars(text).replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


class RailIcon(Static):
    def __init__(self, key: str, icon: str, tip: str) -> None:
        super().__init__(icon, classes="rail-icon", id=f"rail-{key}")
        self.key = key
        self.tooltip = tip

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app.navigate(self.key)  # type: ignore[attr-defined]


class Rail(Vertical):
    def compose(self) -> ComposeResult:
        for key, icon, tip in RAIL:
            yield RailIcon(key, icon, tip)
        yield Static(classes="rail-spacer")
        yield RailIcon("settings", "⚙", "Settings")

    def set_active(self, key: str) -> None:
        for icon in self.query(RailIcon):
            icon.set_class(icon.key == key, "-active")


class NewChatButton(Static):
    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app.action_new_chat()  # type: ignore[attr-defined]


def recent_options(records: list, limit: int = 30) -> list[Option]:
    """Bug cards for the sidebar: coloured severity bar, id, title, status · patterns."""
    out = []
    for r in list(reversed(records))[:limit]:
        bar = f"[{SEV_STYLE.get(r.severity.value, '$text-muted')}]▌[/]"
        pats = ", ".join(r.pattern_ids) or "no pattern"
        prompt = (f"{bar} [b]{r.id}[/]  [$text-muted]{r.severity.value}[/]\n"
                  f"{bar} {escape(_clip(r.title, 21))}\n"
                  f"{bar} [$text-muted]{r.status.value} · {escape(_clip(pats, 12))}[/]")
        out.append(Option(prompt, id=r.id))
    return out


class ContextPanel(VerticalScroll):
    """Right-hand panel: provider, knowledge counts, severity mix, pending draft, project."""

    def compose(self) -> ComposeResult:
        yield Static("CONTEXT", classes="ctx-head")
        yield Static("", id="ctx-status")
        yield Static("AGENT", classes="ctx-label")
        yield Static("", id="ctx-agent", classes="ctx-card")
        yield Static("KNOWLEDGE", classes="ctx-label")
        yield Static("", id="ctx-knowledge")
        yield Static("SEVERITY", classes="ctx-label")
        yield Static("", id="ctx-severity")
        yield Static("DRAFT", classes="ctx-label")
        yield Static("", id="ctx-draft")
        yield Static("WORKSPACE", classes="ctx-label")
        yield Static("", id="ctx-project")

    def update_status(self, app) -> None:
        cfg = app.service.cfg
        chat = app.chat
        label = {"ready": "Ready", "working": "Analyzing…", "error": "Last run failed", "none": "No provider"}[app.status]
        color = {"ready": "$success", "working": "$warning"}.get(app.status, "$error")
        self.query_one("#ctx-status", Static).update(
            f"[b {color}]{label}[/]\n[$text-muted]mode[/] {'bug analysis' if chat.mode == 'bug' else 'auto-detect'}")
        name = app.provider_label()
        initial = name[:1].upper() if name != "none" else "?"
        self.query_one("#ctx-agent", Static).update(
            f"[b $background on $primary] {initial} [/]  [b]{escape(name)}[/]\n"
            f"     {STATUS_DOT[app.status]} [$text-muted]{app.status} · no tools[/]")
        draft = chat.draft
        if draft and draft.is_bug and draft.response.bug:
            bug = draft.response.bug
            q = len(bug.clarification_questions)
            text = (f"[b $warning]● pending[/]  [$text-muted]{bug.severity.value}[/]\n{escape(_clip(bug.title, 30))}\n"
                    f"[$text-muted]{q} open question{'s' if q != 1 else ''} · /save · /discard[/]")
        else:
            text = "[$text-muted]none[/]"
        self.query_one("#ctx-draft", Static).update(text)
        self.query_one("#ctx-project", Static).update(
            f"[$text-muted]project[/]   [b]{escape(cfg.storage.project)}[/]\n"
            f"[$text-muted]template[/]  {escape(_clip(cfg.output.template.split('/')[-1], 18))}\n"
            f"[$text-muted]auto-save[/] {cfg.analysis.auto_save}")

    def update_knowledge(self, k) -> None:
        if k is None:
            self.query_one("#ctx-knowledge", Static).update("[$error]archive unavailable[/]")
            return
        rows = [("Bugs", len(k.records)), ("Patterns", len(k.patterns)),
                ("Active rules", len(k.active_rules)), ("Candidates", len(k.candidate_rules))]
        self.query_one("#ctx-knowledge", Static).update(
            "\n".join(f"{name:<14}[b]{n:>5}[/]" for name, n in rows))
        counts = Counter(r.severity.value for r in k.records)
        top = max(counts.values(), default=0)
        lines = []
        for sev in ("critical", "high", "medium", "low"):
            n = counts.get(sev, 0)
            fill = round(14 * n / top) if top else 0
            lines.append(f"{sev:<9}[{SEV_STYLE[sev]}]{'━' * fill}[/][$panel-lighten-2]{'━' * (14 - fill)}[/] {n:>3}")
        self.query_one("#ctx-severity", Static).update("\n".join(lines))


class TopBar(Static):
    def refresh_info(self, app) -> None:
        cfg = app.service.cfg
        self.update(f"{STATUS_DOT[app.status]} [b]BugMind[/]   [$text-muted]⎇ {escape(cfg.storage.project)}[/]"
                    f"   [$primary]{escape(app.provider_label())}[/]")


class StatusBar(Horizontal):
    def compose(self) -> ComposeResult:
        yield Static("", id="status-left")
        yield Static("", id="status-right")

    def refresh_info(self, app, bugs: int | None) -> None:
        mode = "Bug analysis" if app.chat.mode == "bug" else "Auto-detect"
        sep = "  [$panel-lighten-2]│[/]  "
        self.query_one("#status-left", Static).update(
            f"{STATUS_DOT[app.status]} [$text-muted]{'–' if bugs is None else bugs} bugs[/]{sep}"
            f"[$success]●[/] {mode}{sep}[$primary]{escape(app.provider_label())}[/] [$text-muted]{app.status}[/]")
        self.query_one("#status-right", Static).update(
            f"[$text-muted]^p provider  ^n new  / commands  f1 help[/]{sep}"
            f"[$text-muted]{escape(app.service.cfg.storage.project)}[/]{sep}[$text-muted]v{__version__}[/]")


# ----------------------------------------------------------------------------- clipboard

def _native_copy(text: str) -> bool:
    """Copy with a native helper (fixed argv, no shell). GNOME Terminal ignores OSC 52."""
    import os
    import shutil
    import subprocess

    helpers = []
    if os.environ.get("WAYLAND_DISPLAY"):
        helpers.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        helpers += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for argv in helpers:
        if shutil.which(argv[0]):
            try:
                # DEVNULL outputs: xclip stays alive to serve the selection and must not hold our pipes.
                subprocess.run(argv, input=text.encode(), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=3, check=True)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def copy_text(app, text: str, what: str = "Report", timeout: float | None = None) -> None:
    """Copy in a worker thread (a hung X/Wayland helper must not freeze the UI), then notify.
    Falls back to Textual's OSC 52 copy for terminals that support it."""
    def done(ok: bool) -> None:
        if not ok:
            app.copy_to_clipboard(text)
        app.notify(f"{what} copied to the {'clipboard' if ok else 'terminal clipboard (OSC 52)'}.", title="Copied",
                   **({"timeout": timeout} if timeout else {}))

    def job() -> None:
        ok = _native_copy(text)
        app.call_from_thread(done, ok)

    app.run_worker(job, thread=True, group="clipboard")


# ----------------------------------------------------------------------------- bug detail popup


class BugDetailScreen(ModalScreen[None]):
    """Read a saved bug and act on it without leaving the current screen."""

    BINDINGS = [Binding("escape,q", "dismiss(None)", "Close"), Binding("y", "copy", "Copy"),
                Binding("c", "status('confirmed')", "Confirm"), Binding("x", "status('invalid')", "Invalid")]

    def __init__(self, bug_id: str) -> None:
        super().__init__()
        self.bug_id = bug_id

    def compose(self) -> ComposeResult:
        with Vertical(id="modal", classes="bug-detail"):
            yield Static("", id="bug-head", classes="modal-title")
            with VerticalScroll(id="bug-body"):
                yield Markdown("", id="bug-md", open_links=False)
            with Horizontal(classes="buttons"):
                yield Button("⧉ Copy report  y", id="bug-copy", variant="primary")
                yield Button("✓ Confirm  c", id="bug-confirm")
                yield Button("✕ Invalid  x", id="bug-invalid")
                yield Button("Close  esc", id="bug-close")

    def on_mount(self) -> None:
        self._load()
        self.query_one("#bug-copy").focus()

    def _load(self) -> None:
        rec = self.app.service.store.get(self.bug_id)  # type: ignore[attr-defined]
        self.record = rec
        if rec is None:
            self.query_one("#bug-head", Static).update(f"[b $error]{escape(self.bug_id)} not found[/]")
            return
        sev = rec.severity.value
        self.query_one("#bug-head", Static).update(
            f"[b]{rec.id}[/]  [{SEV_STYLE.get(sev, '$text-muted')}]● {sev}[/]  [$text-muted]{rec.status.value} · "
            f"{escape(', '.join(rec.pattern_ids) or 'no pattern')} · {rec.created_at:%Y-%m-%d %H:%M}[/]")
        shots = "".join(f"\n- Screenshot: `{self.app.service.store.layout.root / rel}`"  # type: ignore[attr-defined]
                        for rel in rec.attachments)
        self.query_one("#bug-md", Markdown).update(strip_control_chars(rec.formatted_report + ("\n\n---" + shots if shots else "")))

    def action_copy(self) -> None:
        if self.record:
            copy_text(self.app, self.record.formatted_report, f"{self.record.id} report")

    def action_status(self, status: str) -> None:
        from bugmind.models import ConfirmationStatus
        if not self.record:
            return
        try:
            self.app.service.set_status(self.record.id, ConfirmationStatus(status))  # type: ignore[attr-defined]
        except Exception as exc:  # storage errors must be visible
            self.app.notify(f"Could not update {self.record.id}: {exc}", severity="error")
            return
        self.app.notify(f"{self.record.id} marked {status}; prevention memory updated.")
        self.app.knowledge_changed()  # type: ignore[attr-defined]
        self._load()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"bug-copy": self.action_copy, "bug-confirm": lambda: self.action_status("confirmed"),
         "bug-invalid": lambda: self.action_status("invalid"),
         "bug-close": lambda: self.dismiss(None)}[event.button.id or "bug-close"]()
