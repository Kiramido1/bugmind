"""Secondary screens: bug history, knowledge views, providers, templates, settings."""

from __future__ import annotations

import shlex
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Markdown, OptionList, Select, Static, Switch, TextArea
from textual.widgets.option_list import Option

from bugmind.security import escape_markup as escape
from bugmind.config import PROVIDER_NAMES, ConfigError, get_value, set_value
from bugmind.models import ConfirmationStatus, Severity
from bugmind.providers import get_provider
from bugmind.security import strip_control_chars
from bugmind.templates import SAMPLE_ANALYSIS, TemplateEngine


def _title(text: str, sub: str = "") -> Static:
    return Static(f"[b]{text}[/b]" + (f"  [dim]{sub}[/dim]" if sub else ""), classes="view-title")


# ----------------------------------------------------------------------------- history


class HistoryView(Vertical):
    def compose(self) -> ComposeResult:
        yield _title("Bug History", "search · filter · Enter to inspect · c confirm · x invalid · u unconfirm")
        with Horizontal(id="history-filters"):
            yield Input(placeholder="Search id, title, endpoint, text…", id="history-search")
            yield Select([(s.value, s.value) for s in Severity], prompt="Severity", id="history-severity")
            yield Select([(s.value, s.value) for s in ConfirmationStatus], prompt="Status", id="history-status")
        with Horizontal(id="history-body"):
            yield DataTable(id="history-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="history-detail"):
                yield Markdown("_Select a bug to inspect it._", id="history-md", open_links=False)

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns("ID", "Title", "Severity", "Category", "Patterns", "Status")
        self.reload()

    def reload(self) -> None:
        svc = self.app.service  # type: ignore[attr-defined]
        q = self.query_one("#history-search", Input).value.strip()
        sev = self.query_one("#history-severity", Select).value
        st = self.query_one("#history-status", Select).value
        recs = svc.store.search(q) if q else svc.store.all()
        recs = [r for r in recs if (not isinstance(sev, str) or r.severity.value == sev)
                and (not isinstance(st, str) or r.status.value == st)]
        t = self.query_one(DataTable)
        keep = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value if t.row_count else None
        t.clear()
        for r in reversed(recs):
            title = strip_control_chars(r.title)[:44] + ("…" if len(r.title) > 44 else "")
            # Text(): DataTable would otherwise parse untrusted titles as Rich markup
            t.add_row(r.id, Text(title), r.severity.value, Text(r.category),
                      ", ".join(r.pattern_ids) or "—", r.status.value, key=r.id)
        if keep is not None and keep in t.rows:  # stay on the same bug after c/x/u or a refresh
            t.move_cursor(row=t.get_row_index(keep))
        if bad := svc.store.corrupt_records():
            self.app.notify(f"{len(bad)} corrupted archive line(s) skipped (first at line {bad[0].line_no}).",
                            severity="warning")

    @on(Input.Changed, "#history-search")
    @on(Select.Changed)
    def _filters(self) -> None:
        self.reload()

    @on(DataTable.RowHighlighted)
    def _show(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        rec = self.app.service.store.get(str(event.row_key.value))  # type: ignore[attr-defined]
        if rec is None:
            return
        a = rec.analysis
        md = [f"## {rec.id}", "",
              f"`{rec.status.value}` · severity `{a.severity.value}` · category `{a.category}` · patterns "
              f"{', '.join(rec.pattern_ids) or 'none'} · rev {rec.revision} · {rec.provider} · "
              f"{rec.created_at:%Y-%m-%d %H:%M} UTC", "", rec.formatted_report]
        if a.clarification_questions:
            md += ["", "### Open questions", *(f"- {q}" for q in a.clarification_questions)]
        if a.hypotheses:
            md += ["", "### Hypotheses (unconfirmed)", *(f"- {h}" for h in a.hypotheses)]
        md += ["", "### Original input", "", "```text", rec.original_input.replace("```", "ˋˋˋ"), "```"]
        self.query_one("#history-md", Markdown).update(strip_control_chars("\n".join(md)))

    def on_key(self, event) -> None:
        mapping = {"c": ConfirmationStatus.CONFIRMED, "x": ConfirmationStatus.INVALID, "u": ConfirmationStatus.UNCONFIRMED}
        t = self.query_one(DataTable)
        if event.key in mapping and t.has_focus and t.row_count:
            event.stop()
            bug_id = str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
            try:
                self.app.service.set_status(bug_id, mapping[event.key])  # type: ignore[attr-defined]
            except Exception as exc:  # storage errors must be visible, not crash the app
                self.app.notify(f"Could not update {bug_id}: {exc}", severity="error")
                return
            self.app.notify(f"{bug_id} → {mapping[event.key].value}; memory updated.")
            self.app.knowledge_changed()  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------- knowledge


class MemoryView(VerticalScroll):
    """Renders one of the Markdown memory files, with Save-to-Downloads and Copy."""

    BINDINGS = [Binding("d", "download", "Save to Downloads"), Binding("y", "copy", "Copy")]

    def __init__(self, filename: str, title: str, sub: str, **kw) -> None:
        super().__init__(**kw)
        self.filename, self.title_text, self.sub = filename, title, sub

    def compose(self) -> ComposeResult:
        yield _title(self.title_text, self.sub)
        with Horizontal(classes="memory-actions"):
            yield Button("⬇ Save to Downloads  d", classes="mem-download", variant="primary", compact=True)
            yield Button("⧉ Copy  y", classes="mem-copy", compact=True)
        yield Markdown("", classes="memory-md", open_links=False)

    def action_download(self) -> None:
        from bugmind.export import TARGETS, ExportError, downloads_dir, export_knowledge
        what = next(k for k, v in TARGETS.items() if v == self.filename)
        try:
            path = export_knowledge(self.app.service, what, downloads_dir(), unique=True)  # type: ignore[attr-defined]
        except (ExportError, OSError) as exc:
            self.app.notify(f"Could not save: {exc}", severity="error")
            return
        self.app.notify(str(path), title=f"Saved {path.name} to Downloads", timeout=8)

    def action_copy(self) -> None:
        from bugmind.tui.panels import copy_text
        copy_text(self.app, self.app.service.memory_text(self.filename), self.filename)  # type: ignore[attr-defined]

    @on(Button.Pressed, ".mem-download")
    def _download(self) -> None:
        self.action_download()

    @on(Button.Pressed, ".mem-copy")
    def _copy(self) -> None:
        self.action_copy()

    def reload(self) -> None:
        text = self.app.service.memory_text(self.filename)  # type: ignore[attr-defined]
        self.query_one(Markdown).update(strip_control_chars(text))


# ----------------------------------------------------------------------------- providers


class ProvidersView(VerticalScroll):
    def compose(self) -> ComposeResult:
        yield _title("AI Providers", "Ctrl+P to switch · availability is checked without calling any model")
        yield DataTable(id="providers-table", cursor_type="row")
        yield Label("Custom agent command (argv; parsed like a shell line but never run through a shell):")
        yield Input(id="custom-command", placeholder="/usr/local/bin/my-agent --json")
        yield Label("Custom agent input mode:")
        yield Select([("stdin_json", "stdin_json"), ("stdin_text", "stdin_text"), ("file", "file")],
                     id="custom-mode", allow_blank=False)
        yield Label("Claude Code model (blank = default):")
        yield Input(id="claude-model")
        yield Label("Claude Code effort (lower = faster):")
        yield Select([(e, e) for e in ("low", "medium", "high", "xhigh", "max")], id="claude-effort",
                     prompt="Claude default")
        yield Label("OpenCode model, provider/model (blank = default):")
        yield Input(id="opencode-model")
        yield Label("Execution timeout (seconds):")
        yield Input(id="provider-timeout", type="integer")
        with Horizontal(classes="buttons"):
            yield Button("Save provider settings", id="providers-save", variant="primary")
            yield Button("Select default provider", id="providers-select")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Provider", "Available", "Version", "Detail", "Default")

    def reload(self) -> None:
        cfg = self.app.service.cfg  # type: ignore[attr-defined]
        self.query_one("#custom-command", Input).value = shlex.join(cfg.provider.custom.command)
        self.query_one("#custom-mode", Select).value = cfg.provider.custom.input_mode
        self.query_one("#claude-model", Input).value = cfg.provider.claude.model or ""
        self.query_one("#claude-effort", Select).value = cfg.provider.claude.effort or Select.NULL
        self.query_one("#opencode-model", Input).value = cfg.provider.opencode.model or ""
        self.query_one("#provider-timeout", Input).value = str(cfg.provider.timeout_seconds)
        self.check()

    @work(exclusive=True, group="provider-check")
    async def check(self) -> None:
        cfg = self.app.service.cfg  # type: ignore[attr-defined]
        t = self.query_one(DataTable)
        t.clear()
        for name in PROVIDER_NAMES:
            s = await get_provider(cfg, name).check()
            t.add_row(Text(s.label), "✓ yes" if s.available else "✗ no", Text(s.version or ""), Text(s.detail[:60]),
                      "●" if cfg.provider.default == name else "")

    @on(Button.Pressed, "#providers-select")
    def _select(self) -> None:
        self.app.action_select_provider()  # type: ignore[attr-defined]

    @on(Button.Pressed, "#providers-save")
    def _save(self) -> None:
        cfg = self.app.service.cfg  # type: ignore[attr-defined]
        try:
            command = shlex.split(self.query_one("#custom-command", Input).value)
            data = cfg.model_dump(mode="json")
            data["provider"]["custom"]["command"] = command
            data["provider"]["custom"]["input_mode"] = self.query_one("#custom-mode", Select).value
            data["provider"]["claude"]["model"] = self.query_one("#claude-model", Input).value.strip() or None
            data["provider"]["opencode"]["model"] = self.query_one("#opencode-model", Input).value.strip() or None
            effort = self.query_one("#claude-effort", Select).value
            data["provider"]["claude"]["effort"] = None if effort is Select.NULL else effort
            data["provider"]["timeout_seconds"] = int(self.query_one("#provider-timeout", Input).value or 300)
            new = type(cfg).model_validate(data)
        except (ValueError, ConfigError) as exc:
            self.app.notify(f"Invalid settings: {exc}", severity="error")
            return
        if not self.app.apply_config(new):  # type: ignore[attr-defined]
            return
        self.app.notify("Provider settings saved.")
        self.check()


# ----------------------------------------------------------------------------- templates

class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, title: str, message: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.title_text, self.message, self.confirm_label = title, message, confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Static(f"[b $warning]{self.title_text}[/]", classes="modal-title")
            yield Static(self.message)
            with Horizontal(classes="buttons"):
                yield Button(self.confirm_label, id="confirm-yes", variant="error")
                yield Button("Cancel", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class NewTemplateScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, suggestion: str) -> None:
        super().__init__()
        self.suggestion = suggestion

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Static("[b $primary]＋ New template[/]", classes="modal-title")
            yield Static("Template id: lowercase segments separated by '/', e.g. [b]mine/compact[/] or "
                         "[b]projects/<project>/report[/]. It starts as a copy of the highlighted template.")
            yield Input(value=self.suggestion, id="new-template-id")
            yield Static("", id="new-template-error")
            with Horizontal(classes="buttons"):
                yield Button("Create", id="new-template-ok", variant="primary")
                yield Button("Cancel", id="new-template-cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#new-template-ok")
    def _ok(self) -> None:
        value = self.query_one(Input).value.strip().strip("/")
        try:
            TemplateEngine._parts(value)
        except ValueError as exc:
            self.query_one("#new-template-error", Static).update(f"[$error]{escape(str(exc))}[/]")
            return
        self.dismiss(value)

    @on(Button.Pressed, "#new-template-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class TemplatesView(Horizontal):
    """Browse, preview, activate, edit, create and reset report templates."""

    BINDINGS = [
        Binding("ctrl+s", "save_template", "Save template"),
        Binding("escape", "cancel_edit", "Cancel edit", show=False),
        Binding("e", "edit_template", "Edit", show=False),
    ]

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.editing_id: str | None = None
        self.original_text = ""
        self._items: list[tuple[str, Path]] = []
        self._debounce = None

    def compose(self) -> ComposeResult:
        with Vertical(id="templates-left"):
            yield _title("Templates", "Enter activate · e edit")
            yield OptionList(id="templates-list")
            with Horizontal(classes="tpl-buttons"):
                yield Button("✎ Edit", id="tpl-edit", variant="primary")
                yield Button("＋ New", id="tpl-new")
                yield Button("↺ Reset", id="tpl-reset")
            yield Static("", id="templates-path")
        with Vertical(id="templates-right"):
            with Horizontal(id="tpl-bar"):
                yield Static("", id="tpl-status")
                yield Button("Save  Ctrl+S", id="tpl-save", variant="primary")
                yield Button("Cancel  Esc", id="tpl-cancel")
            with Horizontal(id="tpl-work"):
                yield TextArea(id="tpl-editor", language="markdown", show_line_numbers=True, soft_wrap=True,
                               tab_behavior="indent")
                with VerticalScroll(id="templates-preview"):
                    yield Markdown("", id="templates-md", open_links=False)
            yield Static("", id="tpl-error")

    def on_mount(self) -> None:
        self._set_editing(None)

    # ------------------------------------------------------------ helpers
    @property
    def engine(self) -> TemplateEngine:
        return self.app.service.engine  # type: ignore[attr-defined]

    def _highlighted_id(self) -> str | None:
        ol = self.query_one("#templates-list", OptionList)
        if ol.highlighted is None or not self._items:
            return None
        return str(ol.get_option_at_index(ol.highlighted).id)

    def _set_editing(self, template_id: str | None) -> None:
        self.editing_id = template_id
        editing = template_id is not None
        self.query_one("#tpl-editor").display = editing
        self.query_one("#tpl-save").display = editing
        self.query_one("#tpl-cancel").display = editing
        self.query_one("#templates-list", OptionList).disabled = editing
        for bid in ("#tpl-edit", "#tpl-new", "#tpl-reset"):
            self.query_one(bid, Button).disabled = editing
        self.query_one("#tpl-error", Static).update("")
        self.query_one("#templates-left").display = not editing  # give the editor the full width
        self.set_class(editing, "-editing")

    def _status(self, text: str) -> None:
        self.query_one("#tpl-status", Static).update(text)

    def _show_preview(self, rendered: str, tpl) -> None:
        guidance = f"\n\n---\n**AI guidance**\n\n{tpl.guidance}" if tpl.guidance else ""
        self.query_one("#templates-md", Markdown).update(f"{rendered}{guidance}")

    # ------------------------------------------------------------ list
    def reload(self) -> None:
        if self.editing_id is not None:
            return
        active = self.app.service.cfg.output.template  # type: ignore[attr-defined]
        ol = self.query_one(OptionList)
        keep = self._highlighted_id()
        ol.clear_options()
        self._items = [(tid, path) for tid, lang, path in self.engine.list() if lang in ("en", "any")]
        for tid, path in self._items:
            mine = self.engine.user_dir is not None and self.engine.user_dir.resolve() in path.resolve().parents
            mark = "[b $success]●[/]" if tid == active else " "
            tag = "  [$secondary]custom[/]" if mine else ""
            ol.add_option(Option(f"{mark} {tid}{tag}", id=tid))
        if self._items:
            ids = [t for t, _ in self._items]
            ol.highlighted = ids.index(keep) if keep in ids else 0

    @on(OptionList.OptionHighlighted, "#templates-list")
    def _preview(self, event: OptionList.OptionHighlighted) -> None:
        if self.editing_id is not None:
            return
        tid = str(event.option.id)
        try:
            tpl = self.engine.resolve(tid)
            mine = self.engine.is_user_template(tpl)
            self._show_preview(self.engine.render(tpl, SAMPLE_ANALYSIS), tpl)
            self._status(f"[b $primary]Preview[/]  [$text-muted]{escape(tpl.name)}[/]"
                         + ("  [$secondary]· your copy[/]" if mine else "  [$text-muted]· built-in[/]"))
            self.query_one("#templates-path", Static).update(f"[$text-muted]{escape(str(tpl.path))}[/]")
        except Exception as exc:
            self.query_one("#templates-md", Markdown).update(f"**Template error:** {exc}")

    @on(OptionList.OptionSelected, "#templates-list")
    def _activate(self, event: OptionList.OptionSelected) -> None:
        cfg = set_value(self.app.service.cfg, "output.template", str(event.option.id))  # type: ignore[attr-defined]
        if not self.app.apply_config(cfg):  # type: ignore[attr-defined]
            return
        self.app.notify(f"Active template: {event.option.id}")
        self.reload()

    # ------------------------------------------------------------ editing
    def _open_editor(self, template_id: str, text: str, note: str) -> None:
        self._set_editing(template_id)
        self.original_text = text
        editor = self.query_one("#tpl-editor", TextArea)
        editor.load_text(text)
        editor.focus()
        self._status(f"[b $warning]✎ Editing[/] [b]{escape(template_id)}[/]  [$text-muted]{note}[/]")
        self._validate()

    @on(Button.Pressed, "#tpl-edit")
    def action_edit_template(self) -> None:
        if self.editing_id is not None:
            return
        tid = self._highlighted_id()
        if not tid:
            return
        tpl = self.engine.resolve(tid)
        note = ("· your copy" if self.engine.is_user_template(tpl)
                else "· saves as your copy, built-in untouched")
        self._open_editor(tid, tpl.path.read_text(encoding="utf-8"), note)

    @on(Button.Pressed, "#tpl-new")
    def _new(self) -> None:
        source = self._highlighted_id() or "default/professional"
        project = self.app.service.cfg.storage.project  # type: ignore[attr-defined]

        def created(new_id: str | None) -> None:
            if not new_id:
                return
            try:
                self.engine.resolve(new_id)
                self.app.notify(f"Template {new_id} already exists — choose another id.", severity="error")
                return
            except Exception:
                pass
            text = self.engine.resolve(source).path.read_text(encoding="utf-8")
            self._open_editor(new_id, text, f"· new, from {source}")

        self.app.push_screen(NewTemplateScreen(f"projects/{project}/my_report"), created)

    @on(TextArea.Changed, "#tpl-editor")
    def _changed(self) -> None:
        if self.editing_id is None:
            return
        if self._debounce is not None:
            self._debounce.stop()
        self._debounce = self.set_timer(0.25, self._validate)

    def _validate(self) -> bool:
        text = self.query_one("#tpl-editor", TextArea).text
        try:
            tpl, rendered = self.engine.validate_text(text, self.editing_id or "preview")
        except ValueError as exc:
            self.query_one("#tpl-error", Static).update(f"[b $error]✕[/] {escape(str(exc))}")
            return False
        self._show_preview(rendered, tpl)
        dirty = text != self.original_text
        self.query_one("#tpl-error", Static).update(
            "[$warning]● unsaved changes[/]  [$text-muted]Ctrl+S to save[/]" if dirty else "[$success]✓ valid[/]")
        return True

    def action_save_template(self) -> None:
        if self.editing_id is None:
            return
        text = self.query_one("#tpl-editor", TextArea).text
        try:
            path = self.engine.save_user_template(self.editing_id, text)
        except ValueError as exc:
            self.query_one("#tpl-error", Static).update(f"[b $error]✕ Not saved:[/] {escape(str(exc))}")
            self.app.notify(f"Not saved: {exc}", severity="error", timeout=6)
            return
        saved_id = self.editing_id
        self._set_editing(None)
        self.app.notify(f"Saved {saved_id}\n{path}", title="Template saved")
        self.reload()
        ids = [t for t, _ in self._items]
        if saved_id in ids:
            self.query_one(OptionList).highlighted = ids.index(saved_id)
        self.query_one(OptionList).focus()

    @on(Button.Pressed, "#tpl-save")
    def _save_btn(self) -> None:
        self.action_save_template()

    @on(Button.Pressed, "#tpl-cancel")
    def action_cancel_edit(self) -> None:
        if self.editing_id is None:
            self.app.action_escape()  # type: ignore[attr-defined]
            return
        if self.query_one("#tpl-editor", TextArea).text == self.original_text:
            self._close_editor()
            return

        def answer(discard: bool | None) -> None:
            if discard:
                self._close_editor()
                self.app.notify("Changes discarded.")

        self.app.push_screen(ConfirmScreen("Discard changes?", "Your unsaved template edits will be lost.",
                                           "Discard"), answer)

    def _close_editor(self) -> None:
        self._set_editing(None)
        self.reload()
        self.query_one(OptionList).focus()

    @on(Button.Pressed, "#tpl-reset")
    def _reset(self) -> None:
        tid = self._highlighted_id()
        if not tid:
            return
        tpl = self.engine.resolve(tid)
        if not self.engine.is_user_template(tpl):
            self.app.notify("This is a built-in template — there is no custom copy to reset.", severity="warning")
            return
        has_builtin = self.engine.has_builtin(tid)
        message = (f"Delete your copy of [b]{tid}[/]? The built-in version will be used again." if has_builtin
                   else f"Delete the custom template [b]{tid}[/]? This cannot be undone.")

        def answer(ok: bool | None) -> None:
            if not ok:
                return
            cfg = self.app.service.cfg  # type: ignore[attr-defined]
            if not has_builtin and cfg.output.template == tid:
                # switch away first: never leave the active template pointing at a deleted file
                if not self.app.apply_config(set_value(cfg, "output.template", "default/professional")):  # type: ignore[attr-defined]
                    return
                self.app.notify("Active template reset to default/professional.")
            self.engine.delete_user_template(tid)
            self.app.notify(f"{'Reset' if has_builtin else 'Deleted'} {tid}.")
            self.reload()

        self.app.push_screen(ConfirmScreen("Reset template?" if has_builtin else "Delete template?", message,
                                           "Reset" if has_builtin else "Delete"), answer)


# ----------------------------------------------------------------------------- settings


class SettingsView(VerticalScroll):
    FIELDS = [
        ("storage.project", "Active project (lowercase slug)"),
        ("storage.data_dir", "Storage directory (blank = default, see `bugmind config path`)"),
        ("analysis.duplicate_threshold", "Near-duplicate similarity threshold (0.5–1.0)"),
        ("storage.backups_to_keep", "Memory backups to keep"),
    ]

    def compose(self) -> ComposeResult:
        yield _title("Settings", "saved to config.yaml")
        for key, label in self.FIELDS:
            yield Label(label)
            yield Input(id="set-" + key.replace(".", "-"))
        yield Label("Theme (applies immediately; Save to keep it)")
        yield Select([(name, name) for name in self._themes()], id="set-theme", allow_blank=False)
        yield Label("Auto-save analyzed bugs")
        yield Select([("when complete (no open questions)", "complete"), ("always", "always"), ("never (use /save)", "never")],
                     id="set-auto-save", allow_blank=False)
        with Horizontal(classes="switch-row"):
            yield Switch(id="set-redact")
            yield Label("Redact secrets (tokens, passwords, keys) before analysis and storage")
        yield Static("[dim]Reports are always written in English.[/dim]")
        yield Button("Save settings", id="settings-save", variant="primary")

    def _themes(self) -> list[str]:
        preferred = ["bugmind-crimson", "bugmind-dark", "bugmind-slate", "bugmind-light"]
        return preferred + sorted(t for t in self.app.available_themes if t not in preferred)

    @on(Select.Changed, "#set-theme")
    def _theme_preview(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value in self.app.available_themes:
            self.app.theme = event.value

    def reload(self) -> None:
        cfg = self.app.service.cfg  # type: ignore[attr-defined]
        self.query_one("#set-theme", Select).value = self.app.theme
        for key, _ in self.FIELDS:
            val = get_value(cfg, key)
            self.query_one("#set-" + key.replace(".", "-"), Input).value = "" if val is None else str(val)
        self.query_one("#set-auto-save", Select).value = cfg.analysis.auto_save
        self.query_one("#set-redact", Switch).value = cfg.analysis.redact_secrets

    @on(Button.Pressed, "#settings-save")
    def _save(self) -> None:
        cfg = self.app.service.cfg  # type: ignore[attr-defined]
        try:
            for key, _ in self.FIELDS:
                cfg = set_value(cfg, key, self.query_one("#set-" + key.replace(".", "-"), Input).value.strip())
            cfg = set_value(cfg, "analysis.auto_save", str(self.query_one("#set-auto-save", Select).value))
            cfg = set_value(cfg, "tui.theme", str(self.query_one("#set-theme", Select).value))
            cfg = set_value(cfg, "analysis.redact_secrets", str(self.query_one("#set-redact", Switch).value).lower())
        except ConfigError as exc:
            self.app.notify(f"Invalid setting: {exc}", severity="error", timeout=8)
            return
        if not self.app.apply_config(cfg):  # type: ignore[attr-defined]
            return
        self.app.notify("Settings saved.")

