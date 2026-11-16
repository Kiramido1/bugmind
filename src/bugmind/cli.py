"""Typer CLI. `bugmind` with no command launches the TUI."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bugmind import __version__
from bugmind.config import (
    PROVIDER_LABELS, PROVIDER_NAMES, AppConfig, ConfigError, config_dir, config_path, get_value, load_config,
    project_dir, resolve_key, save_config, set_value,
)
from bugmind.models import ConfirmationStatus
from bugmind.security import strip_control_chars

app = typer.Typer(add_completion=False, no_args_is_help=False, rich_markup_mode="rich",
                  help="BugMind — AI-powered bug intelligence. Run without a command to open the TUI.")
config_app = typer.Typer(help="Show or change configuration.", invoke_without_command=True)
app.add_typer(config_app, name="config")

console = Console()
err = Console(stderr=True)


def _cfg() -> AppConfig:
    try:
        return load_config()
    except ConfigError as exc:
        err.print(f"[red]Config error:[/] {_m(exc)}")
        raise typer.Exit(2)


def _service(cfg: AppConfig | None = None):
    from bugmind.service import BugMindService
    return BugMindService(cfg or _cfg())


def _safe(text: str) -> str:
    return strip_control_chars(text)


def _m(value: object) -> str:
    """Untrusted value (user input, path, exception text) made literal inside Rich markup."""
    return escape(strip_control_chars(str(value)))


def _auto_provider(cfg: AppConfig) -> AppConfig:
    """Use the configured provider if it can run; otherwise an installed Claude Code / OpenCode (saved)."""
    from bugmind.providers import auto_provider
    name = auto_provider(cfg)
    if name is None or name == cfg.provider.default:
        return cfg
    old = cfg.provider.default
    cfg = set_value(cfg, "provider.default", name)
    save_config(cfg)
    why = f"{PROVIDER_LABELS[old]} isn't set up here" if old else "detected on this machine"
    err.print(f"Using {PROVIDER_LABELS[name]} ({why}).", markup=False)
    return cfg


def choose_provider_interactively(cfg: AppConfig) -> AppConfig:
    console.print("\n[bold]Select your AI provider:[/]\n")
    for i, name in enumerate(PROVIDER_NAMES, 1):
        console.print(f"  {i}. {PROVIDER_LABELS[name]}" + (" (configure provider.custom.command)" if name == "custom" else ""))
    choice = typer.prompt("\nChoice", type=typer.IntRange(1, len(PROVIDER_NAMES)), default=1)
    cfg = set_value(cfg, "provider.default", PROVIDER_NAMES[choice - 1])
    save_config(cfg)
    console.print(f"[green]Provider set to {PROVIDER_LABELS[PROVIDER_NAMES[choice - 1]]}[/]")
    return cfg


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context,
         version: bool = typer.Option(False, "--version", "-V", help="Show version and exit.")) -> None:
    if version:
        console.print(f"BugMind {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        from bugmind.tui.app import run_tui
        run_tui()


@app.command()
def init(provider: Optional[str] = typer.Option(None, "--provider", "-p", help="claude | opencode | custom")) -> None:
    """Create configuration, storage directories and user template folder."""
    cfg = _cfg()
    if provider:
        try:
            cfg = set_value(cfg, "provider.default", provider)
        except ConfigError as exc:
            err.print(f"[red]{_m(exc)}[/]")
            raise typer.Exit(2) from None
    else:
        cfg = _auto_provider(cfg)
        if cfg.provider.default is None and sys.stdin.isatty():
            cfg = choose_provider_interactively(cfg)
    path = save_config(cfg)
    for sub in ("agents", "templates"):
        (config_dir() / sub).mkdir(parents=True, exist_ok=True)
    svc = _service(cfg)
    svc.rebuild_memory()
    console.print(Panel.fit(Text(
        f"Config:   {path}\nData:     {project_dir(cfg)}\nTemplates: {config_dir() / 'templates'} (overrides built-ins)\n"
        f"Provider: {PROVIDER_LABELS.get(cfg.provider.default or '', 'not selected')}"),
        title="BugMind initialised", border_style="green"))


@app.command()
def add(
    text: Optional[str] = typer.Argument(None, help="Bug description. Omit to read stdin or open $EDITOR."),
    file: Optional[Path] = typer.Option(None, "--file", "-f", exists=True, dir_okay=False, help="Read description from file."),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Override provider for this run."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save even if the agent asked clarification questions."),
    force: bool = typer.Option(False, "--force", help="Save even if it is an exact duplicate."),
    image: list[Path] = typer.Option([], "--image", "-i", metavar="PATH", help="Screenshot to attach (PNG/JPEG/GIF/WebP, repeatable)."),
) -> None:
    """Analyze a bug with the AI provider and save it to the archive."""
    from bugmind.images import ImageError, check_count, load_image
    try:
        images = [load_image(p) for p in image]
        check_count(images)
    except ImageError as exc:
        err.print(f"[red]{_m(exc)}[/]")
        raise typer.Exit(2) from None
    from bugmind.providers import ProviderError
    from bugmind.analysis import ResponseParseError
    from bugmind.service import DuplicateBugError

    if file:
        text = file.read_text(encoding="utf-8")
    elif text is None and not sys.stdin.isatty():
        text = sys.stdin.read()
    elif text is None:
        text = typer.edit("\n# Describe the bug above (Arabic or English). Lines starting with # are ignored.\n")
        text = "\n".join(ln for ln in (text or "").splitlines() if not ln.startswith("#"))
    if not text or not text.strip():
        err.print("[red]No bug description given.[/]")
        raise typer.Exit(1)

    cfg = _cfg() if provider else _auto_provider(_cfg())
    if not (provider or cfg.provider.default):
        if not sys.stdin.isatty():
            err.print("[red]No AI agent found.[/] Install Claude Code or OpenCode, or run "
                      "`bugmind config set agent custom` with provider.custom.command.")
            raise typer.Exit(2)
        cfg = choose_provider_interactively(cfg)
    svc = _service(cfg)
    received = 0

    async def go():
        nonlocal received
        with console.status("Analyzing bug…") as status:
            def on_chunk(chunk: str) -> None:
                nonlocal received
                received += len(chunk)
                status.update(f"Analyzing bug… received {received:,} chars")
            return await svc.analyze(text, force_bug=True, allow_duplicate=force, provider_name=provider, on_chunk=on_chunk,
                                     images=images)

    try:
        draft = asyncio.run(go())
    except DuplicateBugError as exc:
        err.print(f"[yellow]{_m(exc)}.[/] Use --force to save anyway.")
        raise typer.Exit(3)
    except (ProviderError, ResponseParseError, ValueError) as exc:
        err.print(f"[red]Analysis failed:[/] {_m(exc)}")
        raise typer.Exit(1)

    bug = draft.response.bug
    console.print(Panel(Markdown(_safe(draft.report)), title="Bug report", border_style="cyan"))
    if draft.redactions:
        console.print(f"[yellow]{draft.redactions} secret value(s) were redacted before analysis.[/]")
    for d in draft.duplicates:
        console.print(f"[yellow]Possible duplicate of {d.bug_id} (similarity {d.score:.0%})[/]")
    if bug.clarification_questions:
        console.print("\n[bold yellow]The agent needs more information:[/]")
        for q in bug.clarification_questions:
            console.print(f"  • {_m(q)}")
        if not yes and not (sys.stdin.isatty() and typer.confirm("\nSave now as 'needs_info'?", default=True)):
            console.print("Not saved. Re-run `bugmind add` with the extra details, or use --yes.")
            raise typer.Exit(4)
    try:
        res = svc.save(draft, force=force)
    except DuplicateBugError as exc:
        err.print(f"[yellow]{_m(exc)}.[/] Use --force to save anyway.")
        raise typer.Exit(3)
    pats = ", ".join(res.record.pattern_ids) or "unclassified"
    console.print(f"\n[green]Saved {res.record.id}[/] · patterns: {pats} · status: {res.record.status.value}\n"
                  f"Report: {_m(res.report_path)}")


def _bug_table(records) -> Table:
    t = Table(header_style="bold cyan")
    t.add_column("ID", no_wrap=True)
    t.add_column("Title", min_width=30, overflow="fold")
    for col in ("Severity", "Category", "Patterns", "Status", "Created"):
        t.add_column(col, overflow="ellipsis", max_width=18)
    for r in records:
        # Text(): titles/categories come from the AI and must never be parsed as markup
        t.add_row(r.id, Text(_safe(r.title)), r.severity.value, Text(_safe(r.category)),
                  ", ".join(r.pattern_ids) or "—", r.status.value, f"{r.created_at:%Y-%m-%d}")
    return t


@app.command("list")
def list_bugs(
    severity: Optional[str] = typer.Option(None, "--severity", "-s"),
    category: Optional[str] = typer.Option(None, "--category", "-c"),
    status: Optional[str] = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """List saved bugs (newest last)."""
    svc = _service()
    recs = [r for r in svc.store.all()
            if (not severity or r.severity.value == severity) and (not category or r.category == category)
            and (not status or r.status.value == status)]
    if not recs:
        console.print("No bugs found.")
    else:
        console.print(_bug_table(recs[-limit:]))
    if bad := svc.store.corrupt_records():
        err.print(f"[yellow]Warning: {len(bad)} corrupted line(s) in {_m(svc.store.layout.bugs_file)} were skipped "
                  f"(first at line {bad[0].line_no}).[/]")


@app.command()
def show(bug_id: str, original: bool = typer.Option(False, "--original", help="Also print the original input.")) -> None:
    """Show one bug report."""
    svc = _service()
    rec = svc.store.get(bug_id.strip().upper())
    if rec is None:
        err.print(f"[red]{_m(bug_id)} not found[/]")
        raise typer.Exit(1)
    a = rec.analysis
    meta = (f"**{rec.id}** · revision {rec.revision} · status `{rec.status.value}` · severity `{a.severity.value}` · "
            f"category `{a.category}` · patterns {', '.join(rec.pattern_ids) or 'none'} · provider `{rec.provider}` · "
            f"created {rec.created_at:%Y-%m-%d %H:%M} UTC")
    console.print(Markdown(meta))
    console.print(Panel(Markdown(_safe(rec.formatted_report)), border_style="cyan"))
    for rel in rec.attachments:
        console.print(f"Screenshot: {svc.store.layout.root / rel}", markup=False)
    if a.clarification_questions:
        console.print(Markdown("**Open questions:**\n" + "\n".join(f"- {q}" for q in a.clarification_questions)))
    if a.hypotheses:
        console.print(Markdown("**Hypotheses (unconfirmed):**\n" + "\n".join(f"- {h}" for h in a.hypotheses)))
    if original:
        console.print(Panel(Text(_safe(rec.original_input)), title="Original input", border_style="dim"))


@app.command()
def search(query: str) -> None:
    """Full-text search across bugs."""
    recs = _service().store.search(query)
    console.print(_bug_table(recs) if recs else f"No bugs match {_m(repr(query))}.")


@app.command()
def status(bug_id: str, new_status: ConfirmationStatus = typer.Argument(..., help="confirmed | unconfirmed | needs_info | invalid"),
           note: str = typer.Option("", "--note")) -> None:
    """Change a bug's confirmation status (adds a revision; memory is updated)."""
    try:
        rec = _service().set_status(bug_id, new_status, note)
    except KeyError:
        err.print(f"[red]{_m(bug_id)} not found[/]")
        raise typer.Exit(1)
    console.print(f"[green]{rec.id} → {rec.status.value}[/] (revision {rec.revision}); memory updated.")


def _print_memory(name: str) -> None:
    console.print(Markdown(_safe(_service().memory_text(name))))


@app.command()
def memory(rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild all memory files from the archive.")) -> None:
    """Show BACKEND_PREVENTION.md (the prevention rules)."""
    if rebuild:
        files = _service().rebuild_memory()
        console.print("[green]Rebuilt:[/] " + ", ".join(str(f) for f in files))
        return
    _print_memory("BACKEND_PREVENTION.md")


@app.command()
def patterns() -> None:
    """Show BUG_PATTERNS.md."""
    _print_memory("BUG_PATTERNS.md")


@app.command()
def stats() -> None:
    """Show statistics derived from the archive."""
    _print_memory("STATISTICS.md")


@app.command()
def export(
    what: str = typer.Argument("all", help="prevention | patterns | stats | all"),
    output: Path = typer.Option(Path("."), "--output", "-o", help="Destination directory (or file for single exports)."),
    zip_: bool = typer.Option(False, "--zip", help="Package 'all' as a .zip archive."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    downloads: bool = typer.Option(False, "--downloads", "-d",
                                   help="Save to your Downloads folder (never overwrites: adds ' (1)')."),
) -> None:
    """Export the current knowledge base on demand."""
    from bugmind.export import ExportError, downloads_dir, export_knowledge
    try:
        if downloads:
            path = export_knowledge(_service(), what, downloads_dir(), as_zip=zip_, unique=True)
        else:
            path = export_knowledge(_service(), what, output, as_zip=zip_, force=force)
    except ExportError as exc:
        err.print(f"[red]{_m(exc)}[/]")
        raise typer.Exit(1)
    console.print(f"[green]Exported:[/] {_m(path)}")


@app.command()
def templates() -> None:
    """List available report templates."""
    from bugmind.templates import TemplateEngine
    cfg = _cfg()
    t = Table(header_style="bold cyan")
    for col in ("Template id", "Language", "Path", "Active"):
        t.add_column(col)
    for tid, lang, path in TemplateEngine(config_dir() / "templates").list():
        t.add_row(Text(tid), lang, Text(str(path)), "●" if tid == cfg.output.template else "")
    console.print(t)


@app.command()
def providers() -> None:
    """Check which AI providers are installed (does not call any model)."""
    from bugmind.providers import get_provider
    cfg = _cfg()

    async def check():
        return [await get_provider(cfg, n).check() for n in PROVIDER_NAMES]

    t = Table(header_style="bold cyan")
    for col in ("Provider", "Available", "Version", "Detail", "Default"):
        t.add_column(col)
    for s in asyncio.run(check()):
        t.add_row(Text(s.label), "[green]yes[/]" if s.available else "[red]no[/]", Text(s.version or ""), Text(s.detail),
                  "●" if s.name == cfg.provider.default else "")
    console.print(t)


@config_app.callback()
def config_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        console.print(f"[dim]{_m(config_path())}[/]")
        import yaml
        console.print(yaml.safe_dump(_cfg().model_dump(mode="json"), sort_keys=False, allow_unicode=True), markup=False)


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a value, e.g. `bugmind config set agent claude` or `provider.timeout_seconds 600`."""
    try:
        cfg = set_value(_cfg(), key, value)
        if resolve_key(key) == ["output", "template"]:
            from bugmind.templates import TemplateEngine, TemplateNotFound
            try:
                TemplateEngine(config_dir() / "templates").resolve(cfg.output.template)  # refuse unknown/unsafe ids
            except TemplateNotFound as exc:
                raise ConfigError(f"Unknown template: {exc}") from exc
    except (ConfigError, ValueError) as exc:
        err.print(f"[red]{_m(exc)}[/]")
        raise typer.Exit(2) from None
    save_config(cfg)
    console.print(f"[green]{_m(key)} = {_m(get_value(cfg, key))}[/]")


@config_app.command("get")
def config_get(key: str) -> None:
    """Print one value."""
    try:
        console.print(get_value(_cfg(), key), markup=False)
    except ConfigError as exc:
        err.print(f"[red]{_m(exc)}[/]")
        raise typer.Exit(2)


@config_app.command("path")
def config_path_cmd() -> None:
    """Print config and data locations."""
    cfg = _cfg()
    console.print(f"config: {config_path()}\ndata:   {project_dir(cfg)}", markup=False)


def main() -> None:
    from jinja2 import TemplateError

    from bugmind.storage.atomic import LockTimeout
    from bugmind.templates import TemplateNotFound
    try:
        app()
    except (OSError, LockTimeout, TemplateNotFound, TemplateError) as exc:
        # Operational problems (disk, lock held by another BugMind, broken template) get a clean
        # message instead of a traceback; programming errors still surface normally.
        err.print(f"[red]Error:[/] {_m(exc)}")
        raise SystemExit(1) from None
